import logging
import os
import random
import re
import time

from paramiko import AutoAddPolicy, SSHClient

from opentaskpy.remotehandlers.remotehandler import (
    RemoteExecutionHandler,
    RemoteTransferHandler,
)

logger = logging.getLogger(__name__)

SSH_OPTIONS = "-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5"


class SSHTransfer(RemoteTransferHandler):

    FILE_NAME_DELIMITER = "|||"

    def __init__(self, spec):
        self.spec = spec
        self.ssh_client = None
        self.sftp_connection = None
        self.log_watch_start_row = 0

        client = SSHClient()
        client.set_missing_host_key_policy(AutoAddPolicy())
        self.ssh_client = client

    def connect(self, hostname, ssh_client=None):
        is_remote_host = False
        if ssh_client is not None:
            is_remote_host = True
        else:
            ssh_client = self.ssh_client

        if ssh_client.get_transport() and ssh_client.get_transport().is_active():
            logger.debug(f"SSH connection to {hostname} already active")
            return
        try:
            ssh_client.connect(
                hostname,
                username=self.spec["protocol"]["credentials"]["username"],
                timeout=5,
            )
            _, stdout, _ = ssh_client.exec_command("uname -a")
            with stdout as stdout_fh:
                logger.log(11, f"Remote uname: {stdout_fh.read().decode('UTF-8')}")

            # Transfer over the transfer.py script
            local_script = (
                f"{os.path.dirname(os.path.realpath(__file__))}/scripts/transfer.py"
            )

            sftp = ssh_client.open_sftp()
            sftp.put(local_script, "/tmp/transfer.py")

            if not is_remote_host:
                self.sftp_connection = sftp
        except Exception as e:
            logger.error(f"Unable to connect to {hostname}: {e}")
            raise e

    def tidy(self):
        # Remove remote scripts
        if self.sftp_connection:
            file_list = self.sftp_connection.listdir("/tmp")
            if "transfer.py" in file_list:
                self.sftp_connection.remove("/tmp/transfer.py")
            self.sftp_connection.close()

            logger.debug(f"Closing SSH connection to {self.spec['hostname']}")
            self.ssh_client.close()

    def get_staging_directory(self, remote_spec):
        return (
            remote_spec["stagingDirectory"]
            if "stagingDirectory" in remote_spec
            else f"~/otf/{os.environ['OTF_TASK_ID']}/"
        )

    """
    Determine the list of files that match the source definition
    List remote files based on the source file pattern
    """

    def list_files(self, directory=None, file_pattern=None):

        self.connect(self.spec["hostname"])
        if not directory:
            directory = self.spec["directory"]
        if not file_pattern:
            file_pattern = self.spec["fileRegex"]

        logger.log(
            12, f"Searching in {directory} for files with pattern {file_pattern}"
        )
        remote_files = dict()
        remote_file_list = self.sftp_connection.listdir(directory)
        for file in list(remote_file_list):
            if re.match(file_pattern, file):
                # Get the file attributes
                file_attr = self.sftp_connection.lstat(f"{directory}/{file}")
                logger.log(12, f"File attributes {file_attr}")
                remote_files[f"{directory}/{file}"] = {
                    "size": file_attr.st_size,
                    "modified_time": file_attr.st_mtime,
                }

        return remote_files

    def transfer_files(self, files, remote_spec, dest_remote_handler=None):
        self.connect(self.spec["hostname"])

        # If we are given a destination handler, make sure we connect to the host
        if dest_remote_handler:
            self.connect(remote_spec["hostname"], dest_remote_handler.ssh_client)

        # Construct an SCP command to transfer the files to the destination server
        remote_user = (
            remote_spec["protocol"]["credentials"]["transferUsername"]
            if "transferUsername" in remote_spec["protocol"]["credentials"]
            else remote_spec["protocol"]["credentials"]["username"]
        )
        remote_host = remote_spec["hostname"]
        # Handle staging directory if there is one
        destination_directory = self.get_staging_directory(remote_spec)

        # Create/validate staging directory exists on destination
        remote_command = (
            f"test -e {destination_directory} || mkdir -p {destination_directory}"
        )
        logger.info(f"Validating staging dir via SSH: {remote_command}")
        _, stdout, stderr = dest_remote_handler.ssh_client.exec_command(remote_command)
        with stdout as stdout_fh:
            str_stdout = stdout_fh.read().decode("UTF-8")
            if str_stdout:
                log_stdout(str_stdout, remote_host)

        with stderr as stderr_fh:
            str_stderr = stderr_fh.read().decode("UTF-8")
            if str_stderr and len(str_stderr) > 0:
                logger.info(f"Remote stderr returned:\n{str_stderr}")

        remote_rc = stdout.channel.recv_exit_status()
        logger.info(f"Got return code {remote_rc} from SSH command")

        remote_command = f'scp {SSH_OPTIONS} {" ".join(files)} {remote_user}@{remote_host}:"{destination_directory}"'
        logger.info(f"Transferring files via SCP: {remote_command}")

        _, stdout, stderr = self.ssh_client.exec_command(remote_command)

        with stdout as stdout_fh:
            str_stdout = stdout_fh.read().decode("UTF-8")
            if str_stdout:
                log_stdout(str_stdout, self.spec["hostname"])

        with stderr as stderr_fh:
            str_stderr = stderr_fh.read().decode("UTF-8")
            if str_stderr and len(str_stderr) > 0:
                logger.info(f"Remote stderr returned:\n{str_stderr}")

        remote_rc = stdout.channel.recv_exit_status()
        logger.info(f"Got return code {remote_rc} from SCP command")

        return remote_rc

    def pull_files(self, files, remote_spec):
        self.connect(self.spec["hostname"])
        # Construct an SCP command to transfer the files from the source server
        source_user = self.spec["protocol"]["credentials"]["transferUsername"]
        source_host = remote_spec["hostname"]

        # Handle staging directory if there is one
        destination_directory = self.get_staging_directory(self.spec)

        # Create/validate staging directory exists
        remote_command = (
            f"test -e {destination_directory} || mkdir -p {destination_directory}"
        )
        logger.info(f"Validating staging dir via SSH: {remote_command}")
        _, stdout, stderr = self.ssh_client.exec_command(remote_command)
        with stdout as stdout_fh:
            str_stdout = stdout_fh.read().decode("UTF-8")
            if str_stdout:
                log_stdout(str_stdout, self.spec["hostname"])

        with stderr as stderr_fh:
            str_stderr = stderr_fh.read().decode("UTF-8")
            if str_stderr and len(str_stderr) > 0:
                logger.info(f"Remote stderr returned:\n{str_stderr}")

        remote_rc = stdout.channel.recv_exit_status()
        logger.info(f"Got return code {remote_rc} from SSH command")

        files_str = ""
        for file in files:
            files_str += f"{source_user}@{source_host}:{file} "

        remote_command = (
            f"scp {SSH_OPTIONS} {files_str.strip()} {destination_directory}"
        )
        logger.info(f"Transferring files via SCP: {remote_command}")

        _, stdout, stderr = self.ssh_client.exec_command(remote_command)

        with stdout as stdout_fh:
            str_stdout = stdout_fh.read().decode("UTF-8")
            if str_stdout:
                log_stdout(str_stdout, self.spec["hostname"])

        with stderr as stderr_fh:
            str_stderr = stderr_fh.read().decode("UTF-8")
            if str_stderr and len(str_stderr) > 0:
                logger.info(f"Remote stderr returned:\n{str_stderr}")

        remote_rc = stdout.channel.recv_exit_status()
        logger.info(f"Got return code {remote_rc} from SCP command")

        return remote_rc

    def move_files_to_final_location(self, files):

        self.connect(self.spec["hostname"])

        # Convert all the source file names into the filename with the destination directory as a prefix
        file_names_str = ""
        files_with_directory = []
        for file in list(files):
            files_with_directory.append(
                f"{self.get_staging_directory(self.spec)}{os.path.basename(file)}"
            )
        file_names_str = self.FILE_NAME_DELIMITER.join(files_with_directory).strip()

        # Next step is to move the file to it's final resting place with the correct permissions and ownership
        # Build a commnd to pass to the remote transfer.py to do the work
        owner_args = (
            f"--owner {self.spec['permissions']['owner']}"
            if "permissions" in self.spec and "owner" in self.spec["permissions"]
            else ""
        )
        group_args = (
            f"--group {self.spec['permissions']['group']}"
            if "permissions" in self.spec and "group" in self.spec["permissions"]
            else ""
        )
        mode_args = f"--mode {self.spec['mode']}" if "mode" in self.spec else ""
        rename_args = (
            f"--renameRegex '{self.spec['rename']['pattern']}' --renameSub '{self.spec['rename']['sub']}'"
            if "rename" in self.spec
            else ""
        )
        remote_command = f"python3 /tmp/transfer.py --moveFiles '{file_names_str}' --destination {self.spec['directory']} {owner_args} {group_args} {mode_args} {rename_args}"
        logger.info(f"[{self.spec['hostname']}] - Running: {remote_command}")

        stdin, stdout, stderr = self.ssh_client.exec_command(remote_command)

        with stdout as stdout_fh:
            str_stdout = stdout_fh.read().decode("UTF-8")
            if str_stdout:
                log_stdout(str_stdout, self.spec["hostname"])

        with stderr as stderr_fh:
            str_stderr = stderr_fh.read().decode("UTF-8")
            if str_stderr and len(str_stderr) > 0:
                logger.info(
                    f"[{self.spec['hostname']}] Remote stderr returned:\n{str_stderr}"
                )

        remote_rc = stdout.channel.recv_exit_status()
        logger.info(
            f"[{self.spec['hostname']}] Got return code {remote_rc} from SSH move command"
        )
        return remote_rc

    def handle_post_copy_action(self, files):
        self.connect(self.spec["hostname"])

        remote_command = None
        if self.spec["postCopyAction"]["action"] == "delete":
            remote_command = f"python3 /tmp/transfer.py --deleteFiles '{self.FILE_NAME_DELIMITER.join(files)}'"
        if self.spec["postCopyAction"]["action"] == "move":
            remote_command = f"python3 /tmp/transfer.py --moveFiles '{self.FILE_NAME_DELIMITER.join(files)}' --destination {self.spec['postCopyAction']['destination']}"

        if remote_command:
            logger.info(f"[{self.spec['hostname']}] - Running: {remote_command}")
            _, stdout, stderr = self.ssh_client.exec_command(remote_command)

            with stdout as stdout_fh:
                str_stdout = stdout_fh.read().decode("UTF-8")
                if str_stdout:
                    log_stdout(str_stdout, self.spec["hostname"])

            with stderr as stderr_fh:
                str_stderr = stderr_fh.read().decode("UTF-8")
                if str_stderr and len(str_stderr) > 0:
                    logger.info(
                        f"[{self.spec['hostname']}] Remote stderr returned:\n{str_stderr}"
                    )

            remote_rc = stdout.channel.recv_exit_status()
            logger.info(
                f"[{self.spec['hostname']}] Got return code {remote_rc} from SSH post copy action command"
            )
            return remote_rc

    def init_logwatch(self):
        self.connect(self.spec["hostname"])

        # There are 2 options for logwatches. One is to watch for new entries, the other is to scan the entire log.
        # Default if not specified is to watch for new entries

        # Determine the log details and check it exists first
        log_file = (
            f"{self.spec['logWatch']['directory']}/{self.spec['logWatch']['log']}"
        )

        # Stat the file
        try:
            _ = self.sftp_connection.lstat(f"{log_file}")
        except FileNotFoundError:
            logger.error(
                f"[{self.spec['hostname']}] Log file {log_file} does not exist"
            )
            return 1
        except PermissionError:
            logger.error(
                f"[{self.spec['hostname']}] Log file {log_file} cannot be accessed"
            )
            return 1

        # Open the existing file and determine the number of rows
        with self.sftp_connection.open(log_file) as log_fh:
            rows = 0
            for rows, _ in enumerate(log_fh):  # noqa #B007
                pass
            logger.log(12, f"[{self.spec['hostname']}] Found {rows+1} lines in log")
            self.log_watch_start_row = rows + 1

        return 0

    def do_logwatch(self):

        self.connect(self.spec["hostname"])

        # Determine if the config requires scanning the entire log, or just from the start_row determine in the init function
        start_row = (
            self.log_watch_start_row
            if "tail" in self.spec["logWatch"] and self.spec["logWatch"]["tail"]
            else 0
        )
        logger.log(
            12, f"[{self.spec['hostname']}] Starting logwatch from row {start_row}"
        )

        # Open the remote log file and parse each line for the pattern
        log_file = (
            f"{self.spec['logWatch']['directory']}/{self.spec['logWatch']['log']}"
        )

        with self.sftp_connection.open(log_file) as log_fh:
            for i, line in enumerate(log_fh):
                # We need to start after the previous line in the log
                if i >= start_row:
                    logger.log(
                        11, f"[{self.spec['hostname']}] Log line: {line.strip()}"
                    )
                    if re.search(self.spec["logWatch"]["contentRegex"], line.strip()):
                        logger.log(
                            12,
                            f"[{self.spec['hostname']}] Found matching line in log: {line.strip()} on line: {i+1}",
                        )
                        return 0

        return 1


def log_stdout(str_stdout, hostname):
    # logger.info(f"[{hostname}] Remote stdout returned:")
    # logger.info(f"[{hostname}] ###########")
    for line in str_stdout.splitlines():
        print(f"[{hostname}] REMOTE OUTPUT: {line}")
    # logger.info(f"[{hostname}] ###########")


class SSHExecution(RemoteExecutionHandler):

    ps_regex = r"(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)"

    def tidy(self):
        logger.debug(f"[{self.remote_host}] Closing SSH connection")
        self.ssh_client.close()

    def __init__(self, remote_host, spec):
        self.remote_host = remote_host
        self.spec = spec
        self.ssh_client = None
        self.remote_pid = None
        self.random = random.randint(
            100000, 999999
        )  # Random number used to make sure when we kill stuff, we always kill the right thing

        self.remote_host = remote_host

        client = SSHClient()
        client.set_missing_host_key_policy(AutoAddPolicy())

        self.ssh_client = client

    def connect(self):
        if (
            self.ssh_client
            and self.ssh_client.get_transport()
            and self.ssh_client.get_transport().is_active()
        ):
            return

        self.ssh_client.connect(
            self.remote_host,
            username=self.spec["protocol"]["credentials"]["username"],
            timeout=5,
        )
        _, stdout, _ = self.ssh_client.exec_command("uname -a")
        with stdout as stdout_fh:

            output = stdout_fh.read().decode("UTF-8")
            logger.log(11, f"Remote uname: {output}")

    def _get_child_processes(self, parent_pid, process_listing):
        children = []
        for line in process_listing:
            match = re.search(self.ps_regex, line)
            if match:
                if int(match.group(3)) == parent_pid:
                    child_pid = int(match.group(2))
                    # Never add PID 1 or 0!
                    if child_pid == 1 or child_pid == 0:
                        continue
                    logger.debug(f"Found child process with PID: {child_pid}")
                    children.append(child_pid)
                    # Recurse to find the children of this child
                    children.extend(
                        self._get_child_processes(child_pid, process_listing)
                    )
        return children

    def kill(self):
        logger.info(f"[{self.remote_host}] Killing remote process")

        self.connect()
        # We know the top level remote PID, we need to get all the child processes associated with it
        _, stdout, _ = self.ssh_client.exec_command("ps -ef")
        process_listing = []
        # Get the process listing
        with stdout as stdout_fh:
            process_listing = stdout_fh.read().decode("UTF-8").splitlines()

        # Now we have this, parse it, find the parent PID, and then all the children
        children = self._get_child_processes(self.remote_pid, process_listing)
        children.append(self.remote_pid)
        logger.info(
            f"[{self.remote_host}] Found {len(children)} child processes to kill - {children}"
        )

        # Now we have the list of children, kill them
        command = f"kill {' '.join([str(x) for x in children])}"
        logger.info(
            f"[{self.remote_host}] Killing remote processes with command: {command}"
        )
        _, stdout, _ = self.ssh_client.exec_command(command)
        # Wait for the command to finish
        while not stdout.channel.exit_status_ready():
            time.sleep(0.1)

        # Disconnect SSH
        self.tidy()

    def execute(self, command):

        # Establish the SSH connection
        try:
            self.connect()

            # Command needs the directory to be changed to appended to it
            command = f"echo __OTF_TOKEN__$$_{self.random}__; cd {self.spec['directory']} && {command}"

            logger.info(f"[{self.remote_host}] Executing command: {command}")
            _, stdout, stderr = self.ssh_client.exec_command(command)

            # Log the stdout and stderr
            for line in iter(lambda: stdout.readline(2048), ""):
                log_stdout(line, self.remote_host)

                # Check the line for the token and pull out the PID
                regex = f"__OTF_TOKEN__(\\d+)_{self.random}__"
                if re.search(regex, line):
                    self.remote_pid = int(re.search(regex, line).group(1))
                    logger.info(f"Found remote PID: {self.remote_pid}")

            with stderr as stderr_fh:
                str_stderr = stderr_fh.read().decode("UTF-8")
                if str_stderr and len(str_stderr) > 0:
                    logger.info(
                        f"[{self.remote_host}] Remote stderr returned:\n{str_stderr}"
                    )

            # Get the return code
            remote_rc = stdout.channel.recv_exit_status()
            self.tidy()
            if remote_rc != 0:
                logger.error(
                    f"[{self.remote_host}] Got return code {remote_rc} from SSH command"
                )
                return False
            else:
                return True
        except Exception as e:
            logger.error(f"[{self.remote_host}] Exception caught: {e}")
            return False
