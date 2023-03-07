import json
import os

from fixtures.ssh_clients import *  # noqa:F401
from pytest_shell import fs

from opentaskpy.config.loader import ConfigLoader
from opentaskpy.taskhandlers import transfer

os.environ["OTF_NO_LOG"] = "1"
os.environ["OTF_LOG_LEVEL"] = "DEBUG"

# Create a task definition
email_task_definition = {
    "type": "transfer",
    "source": {
        "hostname": "172.16.0.11",
        "directory": "/tmp/testFiles/src",
        "fileRegex": ".*emailhandler.*\\.txt",
        "protocol": {"name": "ssh", "credentials": {"username": "application"}},
    },
    "destination": [
        {
            "recipients": ["test@example.com" "test1@example.com"],
            "subject": "Test Email Subject",
            "protocol": {
                "name": "email",
                "credentials": {
                    "username": "{{ lookup('file', path='/tmp/smtp_username') }}",
                    "password": "{{ lookup('file', path='/tmp/smtp_password') }}",
                },
                "sender": "Test Sender <test@example.com>",
            },
        },
    ],
}


def test_remote_handler(setup_ssh_keys):
    # Validate that given a transfer with email protocol, that we get a remote handler of type EmailTransfer

    transfer_obj = transfer.Transfer(None, "email-basic", email_task_definition)

    transfer_obj._set_remote_handlers()

    # Validate some things were set as expected
    assert transfer_obj.source_remote_handler.__class__.__name__ == "SSHTransfer"

    # dest_remote_handler should be an array
    assert isinstance(transfer_obj.dest_remote_handlers, list)
    assert len(transfer_obj.dest_remote_handlers) == 1
    #  of SSHTransfer objects
    assert transfer_obj.dest_remote_handlers[0].__class__.__name__ == "EmailTransfer"


def test_remote_handler_vars():
    # Load the global config
    config_loader = ConfigLoader("test/cfg")
    global_variables = config_loader.get_global_variables()
    global_variables["global_protocol_vars"] = [
        {"name": "email", "smtp_port": 587, "smtp_server": "smtp.gmail.com"}
    ]

    transfer_obj = transfer.Transfer(
        global_variables, "email-basic", email_task_definition
    )
    transfer_obj._set_remote_handlers()

    # Validate some things were set as expected
    assert transfer_obj.source_remote_handler.__class__.__name__ == "SSHTransfer"

    # dest_remote_handler should be an array
    assert isinstance(transfer_obj.dest_remote_handlers, list)
    assert len(transfer_obj.dest_remote_handlers) == 1
    #  of SSHTransfer objects
    assert transfer_obj.dest_remote_handlers[0].__class__.__name__ == "EmailTransfer"

    # Check that the transfer object has an SMTP server config from the global config
    assert (
        transfer_obj.dest_remote_handlers[0].protocol_vars["smtp_server"]
        == "smtp.gmail.com"
    )


def test_email_transfer(setup_ssh_keys, root_dir):
    # Dont run this test if the env var GITHUB_ACTIONS is set
    if not os.getenv("GITHUB_ACTIONS"):
        return

    # Create a file to transfer
    fs.create_files(
        [{f"{root_dir}/testFiles/ssh_1/src/emailhandler.txt": {"content": "test1234"}}]
    )

    # Write the email_task_definition to a file which we will read in to resolve the templated values for username and password
    task_definition_file = f"{root_dir}/cfg/transfers/email-transfer.json"
    # Delete the file if it exists
    if os.path.exists(task_definition_file):
        os.remove(task_definition_file)

    fs.create_files(
        [{task_definition_file: {"content": json.dumps(email_task_definition)}}]
    )

    # Load the global config
    config_loader = ConfigLoader("test/cfg")
    global_variables = config_loader.get_global_variables()
    global_variables["global_protocol_vars"] = [
        {"name": "email", "smtp_port": 587, "smtp_server": "smtp.gmail.com"}
    ]

    # Load the task definition using the config_loader
    imported_task_def = config_loader.load_task_definition("email-transfer")
    os.remove(task_definition_file)

    transfer_obj = transfer.Transfer(
        global_variables, "email-transfer", imported_task_def
    )
    transfer_obj._set_remote_handlers()

    # Run the transfer
    transfer_obj.run()
