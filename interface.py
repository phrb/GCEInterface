import socket
import pickle
import sys
import time
import logging

from six.moves import input
from oauth2client.client import GoogleCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import *
from time import sleep
from server_codes import *

class GCEInterface:
    def send_command(self, sock, command, arg1, arg2, arg3):
        response = []
        sock.sendall("{0} {1} {2} {3}".format(command, arg1, arg2, arg3))
        logging.info("Sending: {0} {1} {2} {3}".format(command, arg1, arg2, arg3))

        response.append((sock.recv(self.buffer_size).strip()).split(" "))
        response.append((sock.recv(self.buffer_size).strip()).split(" "))
        logging.info("Received: {0}".format(response))

        return response

    def send_simple_command(self, sock, command, arg):
        response = []
        sock.sendall("{0} {1}".format(command, arg))
        logging.info("Sending: {0} {1}".format(command, arg))

        response.append((sock.recv(self.buffer_size).strip()).split(" "))
        logging.info("Received: {0}".format(response))

        return response

    def clone(self, sock, repo, dest):
        response = self.send_command(sock, CLONE, repo, dest, "")
        return response

    def load(self, sock, path, name):
        response = self.send_command(sock, LOAD, path, name, "")
        return response

    def measure(self, sock, config, config_input, limit):
        response = self.send_command(sock, MEASURE, config, config_input, limit)
        return response

    def start(self, sock):
        response = self.send_simple_command(sock, START, "")
        return response

    def stop(self, sock):
        response = self.send_simple_command(sock, STOP, "")
        return response

    def disconnect(self, sock):
        response = self.send_simple_command(sock, DISCONNECT, "")
        sock.close()
        return response

    def status(self, sock):
        response = self.send_simple_command(sock, STATUS, "")
        return response

    def shutdown(self, sock):
        response = self.send_simple_command(sock, SHUTDOWN, "")
        sock.close()
        return response

    def get(self, sock, result_id):
        response = self.send_simple_command(sock, GET, result_id)
        return response

    def is_ready(self, request, results):
        request_id = request[0]
        target     = request[1]

        logging.info("Checking result {0} on worker {1}.".format(request_id, target))
        sock = self.sockets[target]

        response = self.get(sock, request_id)

        if int(response[0][1]) == NO_ERROR and response[0][3] == request_id:
            logging.info("Result was ready.")
            result = pickle.loads(eval(response[0][4]))
            results.append(result)
            return True
        else:
            logging.info("Result was not ready.")
            return False

    def compute_results(self, args):
        requests = []
        results  = []

        logging.info("Starting to compute the results.")
        logging.info("Sending requests...")
        for i in range(len(args)):
            config     = pickle.dumps(args[i][0])
            c_input    = pickle.dumps(args[i][1])
            limit      = args[i][2]

            target     = i % (len(self.sockets))
            sock       = self.sockets[target]

            response   = self.measure(sock, repr(config),
                                      repr(c_input), limit)

            while int(response[0][1]) != NO_ERROR:
                logging.info("Measure returned an error: {0}".format(response[0]))
                response = self.measure(sock, repr(config),
                                        repr(c_input), limit)
                logging.info("Trying again...")

            request_id = response[1][3]

            logging.info("Sent request {0} to worker {1}.".format(request_id, target))
            requests.append((request_id, target))

        logging.info("Done.")
        logging.info("Waiting for results...")
        while len(requests) > 0:
            requests[:] = [r for r in requests if not self.is_ready(r, results)]

        logging.info("Done.")
        return results

    def list_instances(self):
        result = self.compute.instances().list(project=self.project, zone=self.zone).execute()
        return result['items']

    def add_firewall_tcp_rule(self):
        logging.info("Checking for firewall \"allow-tcp\" rule.")
        try:
            firewall = self.compute.firewalls().get(project=self.project,
                                               firewall='allow-tcp').execute()
        except HttpError, err:
            logging.info("Adding TCP rule to self.project {0}.".format(self.project))
            config = {
                'kind': 'compute#firewall',
                'name': 'allow-tcp',
                'sourceRanges': [ '0.0.0.0/0' ],
                'allowed': [{
                              'IPProtocol': 'tcp',
                              'ports': [ self.tcp_port ]
                           }]
            }
            self.compute.firewalls().insert(project = self.project,
                                            body    = config).execute()
            return
        logging.info("Project {0} already had a TCP rule.".format(self.project))

    def create_instance(self, name):
        source_disk_image = \
            "projects/just-clover-107416/global/images/debian8-opentuner-ready"
        machine_type = "zones/%s/machineTypes/n1-standard-1" % self.zone
        startup_script = open('startup-script.sh', 'r').read()

        config = {
            'name': name,
            'machineType': machine_type,
            # Specify the boot disk and the image to use as a source.
            'disks': [
                {
                    'boot': True,
                    'autoDelete': True,
                    'initializeParams': {
                        'sourceImage': source_disk_image,
                    }
                }
            ],
            # Specify a network interface with NAT to access the public
            # internet.
            'networkInterfaces': [{
                'network': 'global/networks/default',
                'accessConfigs': [
                    {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
                ]
            }],
            # Allow the instance to access cloud storage and logging.
            'serviceAccounts': [{
                'email': 'default',
                'scopes': [
                    'https://www.googleapis.com/auth/devstorage.read_write',
                    'https://www.googleapis.com/auth/logging.write'
                ]
            }],
            # Metadata is readable from the instance and allows you to
            # pass configuration from deployment scripts to instances.
            'metadata': {
                'items': [{
                    # Startup script is automatically executed by the
                    # instance upon startup.
                    'key': 'startup-script',
                    'value': startup_script
                }, {
                    # Every project has a default Cloud Storage bucket that's
                    # the same name as the project.
                    'key': 'bucket',
                    'value': self.project
                }]
            }
        }
        return self.compute.instances().insert(
            project = self.project,
            zone    = self.zone,
            body    = config).execute()

    def delete_instance(self, name):
        return self.compute.instances().delete(
            project  = self.project,
            zone     = self.zone,
            instance = name).execute()

    def wait_for_operation(self, operations):
        logging.info("Waiting for operations to finish...")
        while True:
            results = [self.compute.zoneOperations().get(
                project   = self.project,
                zone      = self.zone,
                operation = operation).execute() for operation in operations ]

            if all(result['status'] == 'DONE' for result in results):
                logging.info("Done.")
                for result in results:
                    if 'error' in result:
                        raise Exception(result['error'])
                return result
            else:
                time.sleep(1)

    def get_natIP(self, instance, interface = 0, config = 0):
        return instance['networkInterfaces'][interface]['accessConfigs'][config]['natIP']

    def start_server(self, instance_ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        server_address = (str(instance_ip), self.tcp_port)
        logging.info("Connecting to {0} : {1}".format(instance_ip, self.tcp_port))

        for _ in range(self.attempts):
            try:
                sock.connect(server_address)
            except Exception as e:
                if e.errno == 106:
                    logging.info("Connected.")
                    break
                else:
                    logging.info("Couldn't connect ({0}), trying again.".format(e.errno))
                    sleep(self.delay)
                    pass

        self.start(sock)
        self.clone(sock, self.repo, self.dest)
        self.load(sock, self.interface_path, self.interface_name)

        return sock


    def create_all(self):
        self.add_firewall_tcp_rule()

        logging.info("Creating instances.")
        operations = []

        for i in range(self.instance_number):
            operations.append(self.create_instance("instance-{0}".format(i))['name'])
            logging.info("Creating instance-{0}.".format(i))

        self.wait_for_operation(operations)

        self.instances = self.list_instances()

    def connect_all(self):
        logging.info("Connecting to instances.")
        self.sockets = []

        logging.info("Instances in project {0} and zone {1}:".format(self.project, self.zone))

        for instance in self.instances:
            logging.info("Instance running at IP: ")
            logging.info(" - {0}".format(self.get_natIP(instance)))

            self.sockets.append(self.start_server(self.get_natIP(instance)))

        logging.info("Checking for instances' server status.")
        for sock in self.sockets:
            self.status(sock)

    def disconnect_all(self):
        for sock in self.sockets:
            self.disconnect(sock)

        self.sockets = []

    def shutdown_all(self):
        for sock in self.sockets:
            self.shutdown(sock)

        self.sockets = []

    def delete_all(self):
        logging.info("Deleting all instances.")
        operations = []

        self.shutdown_all()

        for instance in self.instances:
            operations.append(self.delete_instance(instance['name'])['name'])
            logging.info("Deleting: {0}.".format(instance['name']))

        self.wait_for_operation(operations)

    def create_and_connect_all(self):
        self.create_all()
        self.connect_all()

    def __init__(self,
                 zone            = "us-central1-f",
                 repo            = "https://github.com/phrb/autotuning-gce.git",
                 dest            = "tuner",
                 delay           = 2,
                 project         = "just-clover-107416",
                 attempts        = 13,
                 tcp_port        = 8080,
                 buffer_size     = 4096,
                 interface_path  = "tuner/rosenbrock/rosenbrock.py",
                 interface_name  = "Rosenbrock",
                 instance_number = 8):

        logging.basicConfig(filename = "GCEInterface.log",
                            level = logging.DEBUG,
                            filemode = "w",
                            format = "%(asctime)s %(message)s",
                            datefmt = "%d/%m/%Y %I:%M:%S %p")

        logging.info("Initializing GCEInterface.")

        self.zone            = zone
        self.repo            = repo
        self.dest            = dest
        self.delay           = delay
        self.project         = project
        self.attempts        = attempts
        self.tcp_port        = tcp_port
        self.buffer_size     = buffer_size
        self.interface_path  = interface_path
        self.interface_name  = interface_name
        self.instance_number = instance_number

        logging.info("Getting credentials and \"compute\"")

        self.credentials = GoogleCredentials.get_application_default()
        self.compute     = build('compute', 'v1', credentials = self.credentials)

        self.instances   = None
        self.sockets     = None

        logging.info("Initialization Complete.")

