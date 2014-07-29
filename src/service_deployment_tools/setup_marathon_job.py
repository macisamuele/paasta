#!/usr/bin/env python

import os
import sys
import logging
import argparse
from StringIO import StringIO
import service_configuration_lib
from service_deployment_tools import marathon_tools
from service_deployment_tools import bounce_lib
from marathon import MarathonClient
import pycurl
import pysensu_yelp

# Marathon REST API:
# https://github.com/mesosphere/marathon/blob/master/REST.md#post-v2apps

# DO NOT CHANGE ID_SPACER, UNLESS YOU'RE PREPARED TO CHANGE ALL INSTANCES
# OF IT IN OTHER LIBRARIES (i.e. service_configuration_lib).
# It's used to compose a job's full ID from its name, instance, and iteration.
ID_SPACER = marathon_tools.ID_SPACER
log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description='Creates marathon jobs.')
    parser.add_argument('service_instance',
                        help="The marathon instance of the service to create or update",
                        metavar="SERVICE.INSTANCE")
    parser.add_argument('-d', '--soa-dir', dest="soa_dir", metavar="SOA_DIR",
                        default=service_configuration_lib.DEFAULT_SOA_DIR,
                        help="define a different soa config directory")
    parser.add_argument('-v', '--verbose', action='store_true',
                        dest="verbose", default=False)
    args = parser.parse_args()
    return args


def send_sensu_event(name, instance, soa_dir, status, output):
    rootdir = os.path.abspath(soa_dir)
    monitoring_file = os.path.join(rootdir, name, "monitoring.yaml")
    monitor_conf = service_configuration_lib.read_monitoring(monitoring_file)
    # We don't use compose_job_id here because we don't want to change _ to -
    full_name = 'setup_marathon_job.%s%s%s' % (name, ID_SPACER, instance)
    # runbook = monitor_conf.get('runbook')
    runbook = 'y/rb-marathon'
    team = monitor_conf.get('team')
    if team:
        # We need to remove things that aren't kwargs to send_event
        # so that we can just pass everything else as a kwarg.
        # This means that if monitoring.yaml has an erroneous key,
        # the event won't get emitted at all!
        # We'll need a strict spec in yelpsoa_configs to make sure
        # that doesn't happen.
        if 'runbook' in monitor_conf:
            del monitor_conf['runbook']
        del monitor_conf['team']
        monitor_conf['alert_after'] = -1
        try:
            pysensu_yelp.send_event(full_name, runbook, status, output, team, **monitor_conf)
        except TypeError:
            log.error("Event %s failed to emit! This service's monitoring.yaml has an erroneous key.")
            return


def get_main_marathon_config():
    log.debug("Reading marathon configuration")
    marathon_config = marathon_tools.get_config()
    log.info("Marathon config is: %s", marathon_config)
    return marathon_config


def get_docker_url(registry_uri, docker_image):
    """Compose the docker url.

    Uses the registry_uri (docker_registry) value from marathon_config
    and the docker_image value from a service config to make a Docker URL.
    Checks if the URL will point to a valid image, first, returning a null
    string if it doesn't.

    The URL is prepended with docker:/// per the deimos docs, at
    https://github.com/mesosphere/deimos"""
    s = StringIO()
    c = pycurl.Curl()
    c.setopt(pycurl.URL, str('http://%s/v1/repositories/%s/tags/%s' % (registry_uri,
                                                                       docker_image.split(':')[0],
                                                                       docker_image.split(':')[1])))
    c.setopt(pycurl.WRITEFUNCTION, s.write)
    c.perform()
    if 'error' in s.getvalue():
        log.error("Docker image not found: %s/%s", registry_uri, docker_image)
        return ''
    else:
        docker_url = 'docker:///%s/%s' % (registry_uri, docker_image)
        log.info("Docker URL: %s", docker_url)
        return docker_url


def get_ports(service_config):
    """Gets the number of ports required from the service's marathon configuration.

    Defaults to one port if unspecified.
    Ports are randomly assigned by Mesos.
    This must return an array, as the Marathon REST API takes an
    array of ports, not a single value."""
    num_ports = service_config.get('num_ports')
    if num_ports:
        return [0 for i in range(int(num_ports))]
    else:
        log.warning("'num_ports' not specified in config. One port will be used.")
        return [0]


def get_mem(service_config):
    """Gets the memory required from the service's marathon configuration.

    Defaults to 100 if no value specified in the config."""
    mem = service_config.get('mem')
    if not mem:
        log.warning("'mem' not specified in config. Using default: 100")
    return int(mem) if mem else 100


def get_cpus(service_config):
    """Gets the number of cpus required from the service's marathon configuration.

    Defaults to 1 if no value specified in the config."""
    cpus = service_config.get('cpus')
    if not cpus:
        log.warning("'cpus' not specified in config. Using default: 1")
    return int(cpus) if cpus else 1


def get_constraints(service_config):
    """Gets the constraints specified in the service's marathon configuration.

    Defaults to no constraints if none given."""
    return service_config.get('constraints')


def get_instances(service_config):
    """Get the number of instances specified in the service's marathon configuration.

    Defaults to 1 if not specified in the config."""
    instances = service_config.get('instances')
    if not instances:
        log.warning("'instances' not specified in config. Using default: 1")
    return int(instances) if instances else 1


def get_bounce_method(service_config):
    """Get the bounce method specified in the service's marathon configuration.

    Defaults to brutal if no method specified in the config."""
    bounce_method = service_config.get('bounce_method')
    if not bounce_method:
        log.warning("'bounce_method' not specified in config. Using default: brutal")
    return bounce_method if bounce_method else 'brutal'


def get_marathon_client(url, user, passwd):
    """Get a new marathon client connection in the form of a MarathonClient object.

    Connects to the Marathon server at 'url' with login specified
    by 'user' and 'pass', all from the marathon config."""
    log.info("Connecting to Marathon server at: %s", url)
    return MarathonClient(url, user, passwd)


def create_complete_config(name, url, docker_options, service_marathon_config):
    """Create the configuration that will be passed to the Marathon REST API.

    Currently compiles the following keys into one nice dict:
      id: the ID of the image in Marathon
      cmd: currently the docker_url, seemingly needed by Marathon to keep the container field
      container: a dict containing the docker url and docker launch options. Needed by deimos.
      uris: blank.
    The following keys are retrieved with the get_* functions defined above:
      ports: an array containing the port.
      mem: the amount of memory required.
      cpus: the number of cpus required.
      constraints: the constraints on the Marathon job.
      instances: the number of instances required."""
    complete_config = {'id': name,
                       'container': {'image': url, 'options': docker_options},
                       'uris': []}
    complete_config['ports'] = get_ports(service_marathon_config)
    complete_config['mem'] = get_mem(service_marathon_config)
    complete_config['cpus'] = get_cpus(service_marathon_config)
    complete_config['constraints'] = get_constraints(service_marathon_config)
    complete_config['instances'] = get_instances(service_marathon_config)
    log.info("Complete configuration for instance is: %s", complete_config)
    return complete_config


def deploy_service(name, config, client, bounce_method):
    """Deploy the service with the given name, config, and bounce_method."""
    log.info("Deploying service instance %s with bounce_method %s", name, bounce_method)
    log.debug("Searching for old service instance iterations")
    filter_name = marathon_tools.remove_iteration_from_job_id(name)
    app_list = client.list_apps()
    old_app_ids = [app.id for app in app_list if filter_name in app.id]
    if old_app_ids:  # there's a previous iteration; bounce
        log.info("Old service instance iterations found: %s", old_app_ids)
        try:
            if bounce_method == "brutal":
                bounce_lib.brutal_bounce(old_app_ids, config, client)
            else:
                log.error("bounce_method not recognized: %s. Exiting", bounce_method)
                return (1, "bounce_method not recognized: %s" % bounce_method)
        except IOError:
            log.error("service %s already being bounced. Exiting", filter_name)
            return (1, "Service is taking a while to bounce")
    else:  # there wasn't actually a previous iteration; just deploy it
        log.info("No old instances found. Deploying instance %s", name)
        client.create_app(**config)
    log.info("%s deployed. Exiting", name)
    return (0, 'Service deployed.')


def setup_service(service_name, instance_name, client, marathon_config,
                  service_marathon_config):
    """Setup the service instance given and attempt to deploy it, if possible.

    The full id of the service instance is service_name__instance_name__iteration.
    Doesn't do anything if the full id is already in Marathon.
    If it's not, attempt to find old instances of the service and bounce them."""
    full_id = marathon_tools.compose_job_id(service_name, instance_name, service_marathon_config['iteration'])
    log.info("Setting up service instance for: %s", marathon_tools.remove_iteration_from_job_id(full_id))
    log.info("Desired Marathon instance id: %s", full_id)
    docker_url = get_docker_url(marathon_config['docker_registry'],
                                service_marathon_config['docker_image'])
    if not docker_url:
        log.error("Docker image %s not found. Exiting", service_marathon_config['docker_image'])
        return (1, "Docker image not found: %s" % service_marathon_config['docker_image'])
    complete_config = create_complete_config(full_id, docker_url, marathon_config['docker_options'],
                                             service_marathon_config)
    try:
        log.info("Checking if instance with iteration %s already exists",
                 service_marathon_config['iteration'])
        client.get_app(full_id)
        log.warning("App id %s already exists. Skipping configuration and exiting.", full_id)
        return (0, 'Service was already deployed.')
    except KeyError:
        return deploy_service(full_id, complete_config, client,
                              bounce_method=get_bounce_method(service_marathon_config))


def main():
    """Deploy a service instance to Marathon from a configuration file.

    Usage: python setup_marathon_job.py <service_name> <instance_name> [options]
    Valid options:
      -d, --soa-dir: A soa config directory to read config files from, otherwise uses
                     service_configuration_lib.DEFAULT_SOA_DIR
      -v, --verbose: Verbose output"""
    args = parse_args()
    soa_dir = args.soa_dir
    if args.verbose:
        log.setLevel(logging.INFO)
    else:
        log.setLevel(logging.WARNING)
    try:
        service_name = args.service_instance.split(ID_SPACER)[0]
        instance_name = args.service_instance.split(ID_SPACER)[1]
    except IndexError:
        log.error("Invalid service instance specified. Format is service_name.instance_name.")
        sys.exit(1)

    marathon_config = get_main_marathon_config()
    client = get_marathon_client(marathon_config['url'], marathon_config['user'],
                                 marathon_config['pass'])

    service_instance_config = marathon_tools.read_service_config(service_name, instance_name,
                                                                 marathon_config['cluster'], soa_dir)

    if service_instance_config:
        try:
            status, output = setup_service(service_name, instance_name, client, marathon_config,
                                           service_instance_config)
            sensu_status = pysensu_yelp.Status.CRITICAL if status else pysensu_yelp.Status.OK
            send_sensu_event(service_name, instance_name, soa_dir, sensu_status, output)
            sys.exit(status)
        except (KeyError, TypeError, ValueError) as e:
            log.error(str(e))
            send_sensu_event(service_name, instance_name, soa_dir, pysensu_yelp.Status.CRITICAL, str(e))
            sys.exit(1)
    else:
        error_msg = "Could not read marathon configuration file for %s in cluster %s" % \
                    (args.service_instance, marathon_config['cluster'])
        log.error(error_msg)
        send_sensu_event(service_name, instance_name, soa_dir, pysensu_yelp.Status.CRITICAL, error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
