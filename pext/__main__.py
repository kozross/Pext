#!/usr/bin/env python3

# Copyright (c) 2016 - 2017 Sylvia van Os <sylvia@hackerchick.me>
#
# This file is part of Pext
#
# Pext is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Pext.

This is the main Pext file. It will initialize, run and manage the whole of
Pext.
"""

import argparse
import atexit
import configparser
import json
import os
import platform
import signal
import sys
import threading
import time
import traceback
import webbrowser
import tempfile

from datetime import datetime
from enum import IntEnum
from importlib import reload  # type: ignore
from inspect import getmembers, isfunction, ismethod, signature
from pkg_resources import parse_version
from shutil import rmtree
from subprocess import check_call, CalledProcessError, Popen
try:
    from typing import Dict, List, Optional, Tuple
except ImportError:
    from backports.typing import Dict, List, Optional, Tuple  # type: ignore
from urllib.request import urlopen
from queue import Queue, Empty

from dulwich import porcelain
from dulwich.repo import Repo
from PyQt5.QtCore import QStringListModel, QLocale, QTranslator, Qt
from PyQt5.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QMessageBox, QTextEdit, QVBoxLayout,
                             QStyleFactory, QSystemTrayIcon)
from PyQt5.Qt import QClipboard, QIcon, QObject, QQmlApplicationEngine, QQmlComponent, QQmlContext, QQmlProperty, QUrl
from PyQt5.QtGui import QPalette, QColor

# FIXME: Workaround for https://bugs.launchpad.net/ubuntu/+source/python-qt4/+bug/941826
warn_no_openGL_linux = False
if platform.system() == "Linux":
    try:
        from OpenGL import GL  # NOQA
    except ImportError:
        warn_no_openGL_linux = True


class AppFile():
    """Get access to application-specific files."""

    @staticmethod
    def get_path() -> str:
        """Return the absolute current path."""
        return os.path.dirname(os.path.abspath(__file__))


# Ensure pext_base and pext_helpers can always be loaded by us and the modules
sys.path.append(os.path.join(AppFile.get_path(), 'helpers'))
sys.path.append(os.path.join(AppFile.get_path()))

from pext_base import ModuleBase  # noqa: E402
from pext_helpers import Action, SelectionType  # noqa: E402


class MinimizeMode(IntEnum):
    """A list of possible ways Pext can react on minimization."""

    Normal = 0
    Tray = 1
    NormalManualOnly = 2
    TrayManualOnly = 3


class SortMode(IntEnum):
    """A list of possible ways Pext can sort module entries."""

    Module = 0
    Ascending = 1
    Descending = 2


class ConfigRetriever():
    """Retrieve global configuration entries."""

    def __init__(self) -> None:
        """Initialize the configuration location."""
        # Initialze defaults
        try:
            config_home = os.environ['XDG_CONFIG_HOME']
        except Exception:
            config_home = os.path.join(os.path.expanduser('~'), '.config')

        self.config = {'config_path': os.path.join(config_home, 'pext')}

    def get_setting(self, variable: str) -> str:
        """Get a specific configuration setting."""
        return self.config[variable]

    def get_updatecheck_permission_asked(self) -> bool:
        """Return info on if allowing updates was asked."""
        try:
            with open(os.path.join(self.get_setting('config_path'), 'update_check_enabled'), 'r'):
                return True
        except (FileNotFoundError):
            return False

    def get_updatecheck_permission(self) -> bool:
        """Return info on if update checking is allowed."""
        try:
            with open(os.path.join(self.get_setting('config_path'), 'update_check_enabled'), 'r') as update_check_file:
                result = update_check_file.readline()
                return True if result == str(1) else False
        except (FileNotFoundError):
            return False

    def save_updatecheck_permission(self, granted: bool) -> None:
        """Save the current updatecheck permission status."""
        with open(os.path.join(self.get_setting('config_path'), 'update_check_enabled'), 'w') as update_check_file:
            update_check_file.write(str(int(granted)))

    def get_last_update_check_time(self) -> Optional[datetime]:
        """Get the time of the last update check, returns None if there was never a check."""
        try:
            with open(os.path.join(self.get_setting('config_path'), 'update_check_time'), 'r') as update_check_file:
                return datetime.fromtimestamp(int(float(update_check_file.readline())))
        except (FileNotFoundError):
            return None

    def set_last_update_check_time(self, dt: datetime) -> None:
        """Set the last update check time to the given time."""
        with open(os.path.join(self.get_setting('config_path'), 'update_check_time'), 'w') as update_check_file:
            update_check_file.write(str(dt.timestamp()))


class RunConseq():
    """A simple helper to run several functions consecutively."""

    def __init__(self, functions: List) -> None:
        """Run the given function consecutively."""
        for function in functions:
            if len(function['args']) > 0:
                function['name'](function['args'], **function['kwargs'])
            else:
                function['name'](**function['kwargs'])


class InputDialog(QDialog):
    """A simple dialog requesting user input."""

    def __init__(self, question: str, text: str, parent=None) -> None:
        """Initialize the dialog."""
        super().__init__(parent)

        self.setWindowTitle("Pext")

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(question))
        self.text_edit = QTextEdit(self)
        self.text_edit.setPlainText(text)
        layout.addWidget(self.text_edit)
        button = QDialogButtonBox(QDialogButtonBox.Ok)
        button.accepted.connect(self.accept)
        layout.addWidget(button)

    def show(self) -> Tuple[str, bool]:
        """Show the dialog."""
        result = self.exec_()
        return (self.text_edit.toPlainText(), result == QDialog.Accepted)


class Logger():
    """Log events to the appropriate location.

    Shows events in the main window and, if the main window is not visible,
    as a desktop notification.
    """

    def __init__(self, window: 'Window') -> None:
        """Initialize the logger and add a status bar to the main window."""
        self.window = window
        self.queued_messages = []  # type: List[Dict[str, str]]

        self.last_update = None  # type: Optional[float]
        self.status_text = self.window.window.findChild(QObject, "statusText")
        self.status_queue = self.window.window.findChild(QObject, "statusQueue")

    def _queue_message(self, module_name: str, message: str, type_name: str) -> None:
        """Queue a message for display."""
        for formatted_message in self._format_message(module_name, message):
            self.queued_messages.append(
                {'message': formatted_message, 'type': type_name})

    def _format_message(self, module_name: str, message: str) -> List[str]:
        """Format message for display, including splitting multiline messages."""
        message_lines = []
        for line in message.splitlines():
            if not (not line or line.isspace()):
                if module_name:
                    message = '{}: {}'.format(module_name, line)
                else:
                    message = line

                message_lines.append(message)

        return message_lines

    @staticmethod
    def _log(message: str, logger=None) -> None:
        """If a logger is provided, log to the logger. Otherwise, print."""
        if logger:
            logger.add_message("", message)
        else:
            print(message)

    @staticmethod
    def _log_error(message: str, logger=None) -> None:
        """If a logger is provided, log to the logger. Otherwise, print."""
        if logger:
            logger.add_error("", message)
        else:
            print(message)

    def show_next_message(self) -> None:
        """Show next statusbar message.

        If the status bar has not been updated for 1 second, display the next
        message. If no messages are available, clear the status bar after it
        has been displayed for 5 seconds.
        """
        current_time = time.time()
        time_diff = 5 if len(self.queued_messages) < 1 else 1
        if self.last_update and current_time - time_diff < self.last_update:
            return

        if len(self.queued_messages) == 0:
            QQmlProperty.write(self.status_text, "text", "")
            self.last_update = None
        else:
            message = self.queued_messages.pop(0)

            if message['type'] == 'error':
                statusbar_message = "<font color='red'>⚠ {}</color>".format(
                    message['message'])
                notification_message = '⚠ {}'.format(message['message'])
            else:
                statusbar_message = message['message']
                notification_message = message['message']

            QQmlProperty.write(self.status_text, "text", statusbar_message)

            if self.window.window.windowState() == Qt.WindowMinimized or not self.window.window.isVisible():
                try:
                    Popen(['notify-send', 'Pext', notification_message])
                except Exception as e:
                    print("Could not open notify-send: {}. Notification follows after exception:".format(e))
                    traceback.print_exc()
                    print(notification_message)

            self.last_update = current_time

    def add_error(self, module_name: str, message: str) -> None:
        """Add an error message to the queue."""
        self._queue_message(module_name, message, 'error')

    def add_message(self, module_name: str, message: str) -> None:
        """Add a regular message to the queue."""
        self._queue_message(module_name, message, 'message')

    def set_queue_count(self, count: List[int]) -> None:
        """Show the queue size on screen."""
        QQmlProperty.write(self.status_queue, "entriesLeftForeground", count[0])
        QQmlProperty.write(self.status_queue, "entriesLeftBackground", count[1])


class MainLoop():
    """Main application loop.

    The main application loop connects the application, queue and UI events and
    ensures these events get managed without locking up the UI.
    """

    def __init__(self, app: QApplication, window: 'Window', logger: Logger) -> None:
        """Initialize the main loop."""
        self.app = app
        self.window = window
        self.logger = logger

    def _process_tab_action(self, tab: Dict, active_tab: int) -> None:
        action = tab['queue'].get_nowait()

        if action[0] == Action.critical_error:
            self.logger.add_error(tab['module_name'], str(action[1]))
            tab_id = self.window.tab_bindings.index(tab)
            self.window.module_manager.unload_module(self.window, tab_id)

        elif action[0] == Action.add_message:
            self.logger.add_message(tab['module_name'], str(action[1]))

        elif action[0] == Action.add_error:
            self.logger.add_error(tab['module_name'], str(action[1]))

        elif action[0] == Action.add_entry:
            tab['vm'].entry_list = tab['vm'].entry_list + [action[1]]

        elif action[0] == Action.prepend_entry:
            tab['vm'].entry_list = [action[1]] + tab['vm'].entry_list

        elif action[0] == Action.remove_entry:
            tab['vm'].entry_list.remove(action[1])

        elif action[0] == Action.replace_entry_list:
            if len(action) > 1:
                tab['vm'].entry_list = action[1]
            else:
                tab['vm'].entry_list = []

        elif action[0] == Action.add_command:
            tab['vm'].command_list = tab['vm'].command_list + [action[1]]

        elif action[0] == Action.prepend_command:
            tab['vm'].command_list = [action[1]] + tab['vm'].command_list

        elif action[0] == Action.remove_command:
            tab['vm'].command_list.remove(action[1])

        elif action[0] == Action.replace_command_list:
            if len(action) > 1:
                tab['vm'].command_list = action[1]
            else:
                tab['vm'].command_list = []

        elif action[0] == Action.set_header:
            if len(action) > 1:
                tab['vm'].set_header(str(action[1]))
            else:
                tab['vm'].set_header("")

        elif action[0] == Action.set_filter:
            if len(action) > 1:
                QQmlProperty.write(tab['vm'].search_input_model, "text", str(action[1]))
            else:
                QQmlProperty.write(tab['vm'].search_input_model, "text", "")

        elif action[0] == Action.ask_question_default_yes:
            answer = QMessageBox.question(
                self.window,
                "Pext",
                action[1],
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes)

            if len(signature(tab['vm'].module.process_response).parameters) == 2:
                tab['vm'].module.process_response(
                    True if (answer == QMessageBox.Yes) else False,
                    action[2] if len(action) > 2 else None)
            else:
                tab['vm'].module.process_response(
                    True if (answer == QMessageBox.Yes) else False)

        elif action[0] == Action.ask_question_default_no:
            answer = QMessageBox.question(
                self.window,
                "Pext",
                action[1],
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No)

            if len(signature(tab['vm'].module.process_response).parameters) == 2:
                tab['vm'].module.process_response(
                    True if (answer == QMessageBox.Yes) else False,
                    action[2] if len(action) > 2 else None)
            else:
                tab['vm'].module.process_response(
                    True if (answer == QMessageBox.Yes) else False)

        elif action[0] == Action.ask_input:
            answer, ok = QInputDialog.getText(
                self.window,
                "Pext",
                action[1],
                QLineEdit.Normal,
                action[2] if len(action) > 2 else "")

            if len(signature(tab['vm'].module.process_response).parameters) == 2:
                tab['vm'].module.process_response(
                    answer if ok else None,
                    action[3] if len(action) > 3 else None)
            else:
                tab['vm'].module.process_response(
                    answer if ok else None)

        elif action[0] == Action.ask_input_password:
            answer, ok = QInputDialog.getText(
                self.window,
                "Pext",
                action[1],
                QLineEdit.Password,
                action[2] if len(action) > 2 else "")

            if len(signature(tab['vm'].module.process_response).parameters) == 2:
                tab['vm'].module.process_response(
                    answer if ok else None,
                    action[3] if len(action) > 3 else None)
            else:
                tab['vm'].module.process_response(
                    answer if ok else None)

        elif action[0] == Action.ask_input_multi_line:
            dialog = InputDialog(
                action[1],
                action[2] if len(action) > 2 else "",
                self.window)

            answer, ok = dialog.show()
            if len(signature(tab['vm'].module.process_response).parameters) == 2:
                tab['vm'].module.process_response(
                    answer if ok else None,
                    action[3] if len(action) > 3 else None)
            else:
                tab['vm'].module.process_response(
                    answer if ok else None)

        elif action[0] == Action.copy_to_clipboard:
            # Copy the given data to the user-chosen clipboard
            if Settings.get('clipboard') == 'selection':
                mode = QClipboard.Selection
            else:
                mode = QClipboard.Clipboard

            self.app.clipboard().setText(str(action[1]), mode)

        elif action[0] == Action.set_selection:
            if len(action) > 1:
                tab['vm'].selection = action[1]
            else:
                tab['vm'].selection = []

            tab['vm'].context.setContextProperty(
                "resultListModelDepth", len(tab['vm'].selection))

            tab['vm'].make_selection()

        elif action[0] == Action.close:
            self.window.close()
            tab['vm'].selection = []

            tab['vm'].context.setContextProperty(
                "resultListModelDepth", len(tab['vm'].selection))

            tab['vm'].module.selection_made(tab['vm'].selection)

        elif action[0] == Action.set_entry_info:
            if len(action) > 2:
                tab['vm'].extra_info_entries[str(action[1])] = str(action[2])
            else:
                try:
                    del tab['vm'].extra_info_entries[str(action[1])]
                except KeyError:
                    pass

            tab['vm'].update_context_info_panel(request_update=False)

        elif action[0] == Action.replace_entry_info_dict:
            if len(action) > 1:
                tab['vm'].extra_info_entries = action[1]
            else:
                tab['vm'].extra_info_entries = {}

            tab['vm'].update_context_info_panel(request_update=False)

        elif action[0] == Action.set_command_info:
            if len(action) > 2:
                tab['vm'].extra_info_commands[str(action[1])] = str(action[2])
            else:
                try:
                    del tab['vm'].extra_info_commands[str(action[1])]
                except KeyError:
                    pass

            tab['vm'].update_context_info_panel(request_update=False)

        elif action[0] == Action.replace_command_info_dict:
            if len(action) > 1:
                tab['vm'].extra_info_commands = action[1]
            else:
                tab['vm'].extra_info_commands = {}

            tab['vm'].update_context_info_panel(request_update=False)

        elif action[0] == Action.set_base_info:
            if len(action) > 1:
                tab['vm'].update_base_info_panel(action[1])
            else:
                tab['vm'].update_base_info_panel("")

        elif action[0] == Action.set_entry_context:
            if len(action) > 2:
                tab['vm'].context_menu_entries[str(action[1])] = action[2]
            else:
                try:
                    del tab['vm'].context_menu_entries[str(action[1])]
                except KeyError:
                    pass

        elif action[0] == Action.replace_entry_context_dict:
            if len(action) > 1:
                tab['vm'].context_menu_entries = action[1]
            else:
                tab['vm'].context_menu_entries = {}

        elif action[0] == Action.set_command_context:
            if len(action) > 2:
                tab['vm'].context_menu_commands[str(action[1])] = action[2]
            else:
                try:
                    del tab['vm'].context_menu_commands[str(action[1])]
                except KeyError:
                    pass

        elif action[0] == Action.replace_command_context_dict:
            if len(action) > 1:
                tab['vm'].context_menu_commands = action[1]
            else:
                tab['vm'].context_menu_commands = {}

        elif action[0] == Action.set_base_context:
            if len(action) > 1:
                tab['vm'].context_menu_base = action[1]
            else:
                tab['vm'].context_menu_base = []

        else:
            print('WARN: Module requested unknown action {}'.format(action[0]))

        if active_tab and tab['entries_processed'] >= 100:
            tab['vm'].search(new_entries=True)
            tab['entries_processed'] = 0

        self.window.update()
        tab['queue'].task_done()

    def run(self) -> None:
        """Process actions modules put in the queue and keep the window working."""
        while True:
            self.app.sendPostedEvents()
            self.app.processEvents()
            self.logger.show_next_message()

            current_tab = QQmlProperty.read(self.window.tabs, "currentIndex")
            queue_size = [0, 0]

            all_empty = True
            for tab_id, tab in enumerate(self.window.tab_bindings):
                if not tab['init']:
                    continue

                if tab_id == current_tab:
                    queue_size[0] = tab['queue'].qsize()
                    active_tab = True
                    tab['vm'].context.setContextProperty(
                        "resultListModelHasEntries", True if tab['vm'].entry_list or tab['vm'].command_list else False)
                else:
                    queue_size[1] += tab['queue'].qsize()
                    active_tab = False

                try:
                    self._process_tab_action(tab, active_tab)
                    tab['entries_processed'] += 1
                    all_empty = False
                except Empty:
                    if active_tab and tab['entries_processed']:
                        tab['vm'].search(new_entries=True)

                    tab['entries_processed'] = 0
                except Exception as e:
                    print('WARN: Module {} caused exception {}'.format(tab['module_name'], e))
                    traceback.print_exc()

            self.logger.set_queue_count(queue_size)

            if all_empty:
                if self.window.window.isVisible():
                    time.sleep(0.01)
                else:
                    time.sleep(0.1)


class ProfileManager():
    """Create, remove, list, load and save to a profile."""

    def __init__(self, config_retriever: ConfigRetriever) -> None:
        """Initialize the profile manager."""
        self.profile_dir = os.path.join(config_retriever.get_setting('config_path'), 'profiles')
        self.config_retriever = config_retriever
        self.saved_settings = ['clipboard', 'locale', 'minimize_mode', 'sort_mode', 'theme', 'tray']
        self.enum_settings = ['minimize_mode', 'sort_mode']

    @staticmethod
    def _get_pid_path(profile: str) -> str:
        return os.path.join(tempfile.gettempdir(), 'pext_{}.pid'.format(profile))

    @staticmethod
    def lock_profile(profile: str) -> None:
        """Claim the profile as in-use."""
        pidfile = ProfileManager._get_pid_path(profile)
        pid = str(os.getpid())
        open(pidfile, 'w').write(pid)

    @staticmethod
    def get_lock_instance(profile: str) -> Optional[int]:
        """Get the pid of the current process having a lock, if any."""
        pidfile = ProfileManager._get_pid_path(profile)
        if os.path.isfile(pidfile):
            pid = int(open(pidfile, 'r').read())
            if platform.system() == 'Windows':
                return True

            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None

            return pid

    @staticmethod
    def unlock_profile(profile: str) -> None:
        """Remove the status of the profile currently being in use."""
        pidfile = ProfileManager._get_pid_path(profile)
        os.unlink(pidfile)

    @staticmethod
    def default_profile_name() -> str:
        """Return the default profile name."""
        return "default"

    def create_profile(self, profile: str) -> bool:
        """Create a new empty profile if name not in use already."""
        try:
            os.mkdir(os.path.join(self.profile_dir, profile))
        except OSError:
            return False

        return True

    def rename_profile(self, old_name: str, new_name: str) -> bool:
        """Rename a profile that's currently not in use, if the new name doesn't already exist."""
        if ProfileManager.get_lock_instance(old_name):
            return False

        try:
            os.rename(os.path.join(self.profile_dir, old_name), os.path.join(self.profile_dir, new_name))
        except OSError:
            return False

        return True

    def remove_profile(self, profile: str) -> bool:
        """Remove a profile and all associated data if not in use."""
        if ProfileManager.get_lock_instance(profile):
            return False

        rmtree(os.path.join(self.profile_dir, profile))
        return True

    def list_profiles(self) -> List:
        """List the existing profiles."""
        return os.listdir(self.profile_dir)

    def save_modules(self, profile: str, modules: List[Dict]) -> None:
        """Save the list of open modules and their settings to the profile."""
        config = configparser.ConfigParser()
        for number, module in enumerate(modules):
            name = ModuleManager.add_prefix(module['module_name'])
            settings = {}
            for setting in module['settings']:
                # Only save non-internal variables
                if setting[0] != "_":
                    settings[setting] = module['settings'][setting]

            config['{}_{}'.format(number, name)] = settings

        with open(os.path.join(self.profile_dir, profile, 'modules'), 'w') as configfile:
            config.write(configfile)

    def retrieve_modules(self, profile: str) -> List[Dict]:
        """Retrieve the list of modules and their settings from the profile."""
        config = configparser.ConfigParser()
        modules = []

        config.read(os.path.join(self.profile_dir, profile, 'modules'))

        for module in config.sections():
            settings = {}

            for key in config[module]:
                settings[key] = config[module][key]

            modules.append(
                {'name': module.split('_', 1)[1], 'settings': settings})

        return modules

    def save_settings(self, profile: str) -> None:
        """Save the current settings to the profile."""
        config = configparser.ConfigParser()
        settings_to_store = {}
        for setting in Settings.get_all():
            if setting in self.saved_settings:
                setting_data = Settings.get(setting).value if setting in self.enum_settings else Settings.get(setting)
                if setting_data is not None:
                    settings_to_store[setting] = setting_data

        config['settings'] = settings_to_store

        with open(os.path.join(self.profile_dir, profile, 'settings'), 'w') as configfile:
            config.write(configfile)

    def retrieve_settings(self, profile: str) -> Dict:
        """Retrieve the settings from the profile."""
        config = configparser.ConfigParser()
        settings = {}  # type: Dict[str, Any]

        config.read(os.path.join(self.profile_dir, profile, 'settings'))

        try:
            for setting in config['settings']:
                if setting in self.saved_settings:
                    Settings.set(setting, config['settings'][setting])
        except KeyError:
            pass

        return settings


class ObjectManager():
    """Shared management for modules and themes."""

    @staticmethod
    def list_objects(core_directory: str) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Return a list of objects together with their source."""
        objects = {}

        for directory in os.listdir(core_directory):
            if not os.path.isdir(os.path.join(core_directory, directory)):
                continue

            name = directory
            name = ModuleManager.remove_prefix(name)
            name = ThemeManager.remove_prefix(name)

            try:
                source = UpdateManager.get_remote_url(os.path.join(core_directory, directory))
            except Exception:
                source = None

            try:
                with open(os.path.join(core_directory, directory, "metadata.json"), 'r') as metadata_json:
                    metadata = json.load(metadata_json)
            except (FileNotFoundError, json.decoder.JSONDecodeError):
                print("Object {} lacks a metadata.json file".format(name))
                metadata = {}

            # Add revision last updated time
            if source:
                try:
                    version = UpdateManager.get_version(os.path.join(core_directory, directory))
                    metadata['version'] = version
                except Exception:
                    pass

                try:
                    last_updated = UpdateManager.get_last_updated(os.path.join(core_directory, directory))
                    metadata['last_updated'] = str(last_updated)
                except Exception:
                    pass

            objects[name] = {"source": source, "metadata": metadata}

        return objects


class ModuleManager():
    """Install, remove, update and list modules."""

    def __init__(self, config_retriever: ConfigRetriever) -> None:
        """Initialize the module manager."""
        self.config_retriever = config_retriever
        self.module_dir = os.path.join(self.config_retriever.get_setting('config_path'),
                                       'modules')
        self.module_dependencies_dir = os.path.join(self.config_retriever.get_setting('config_path'),
                                                    'module_dependencies')
        self.logger = None  # type: Optional[Logger]

    @staticmethod
    def add_prefix(module_name: str) -> str:
        """Ensure the string starts with pext_module_."""
        if not module_name.startswith('pext_module_'):
            return 'pext_module_{}'.format(module_name)

        return module_name

    @staticmethod
    def remove_prefix(module_name: str) -> str:
        """Remove pext_module_ from the start of the string."""
        if module_name.startswith('pext_module_'):
            return module_name[len('pext_module_'):]

        return module_name

    def _pip_install(self, module_dir_name: str) -> int:
        """Install module dependencies using pip."""
        module_requirements_path = os.path.join(self.module_dir, module_dir_name, 'requirements.txt')
        module_dependencies_path = os.path.join(self.module_dependencies_dir, module_dir_name)

        if not os.path.isfile(module_requirements_path):
            return 0

        try:
            os.mkdir(module_dependencies_path)
        except OSError:
            # Probably already exists, that's okay
            pass

        # Create the pip command
        pip_command = [sys.executable,
                       '-m',
                       'pip',
                       'install']

        # FIXME: Cheap hack to work around Debian's faultily-patched pip
        if os.path.isfile('/etc/debian_version'):
            pip_command += ['--system']

        pip_command += ['--upgrade',
                        '--target',
                        module_dependencies_path,
                        '-r',
                        module_requirements_path]

        returncode = 0

        # FIXME: Cheap macOS workaround, part 1
        # See https://github.com/pypa/pip/pull/4111#issuecomment-280616124
        if platform.system() == "Darwin":
            with open(os.path.expanduser('~/.pydistutils.cfg'), 'w') as macos_workaround:
                macos_workaround.write('[install]\nprefix=')

        # Actually run the pip command
        try:
            check_call(pip_command)
        except CalledProcessError as e:
            returncode = e.returncode

        # FIXME: Cheap macOS workaround, part 2
        if platform.system() == "Darwin":
            os.remove(os.path.expanduser('~/.pydistutils.cfg'))

        return returncode

    def bind_logger(self, logger: Logger) -> None:
        """Connect a logger to the module manager.

        If a logger is connected, the module manager will log all
        messages directly to the logger.
        """
        self.logger = logger

    def load_module(self, window: 'Window', module: Dict, locale: str) -> bool:
        """Load a module and attach it to the main window."""
        # Append modulePath if not yet appendend
        module_path = os.path.join(self.config_retriever.get_setting('config_path'), 'modules')
        if module_path not in sys.path:
            sys.path.append(module_path)

        # Remove pext_module_ from the module name
        module_dir = ModuleManager.add_prefix(module['name']).replace('.', '_')
        module_name = ModuleManager.remove_prefix(module['name'])

        # Append module dependencies path if not yet appended
        module_dependencies_path = os.path.join(self.config_retriever.get_setting('config_path'),
                                                'module_dependencies',
                                                module_dir)
        if module_dependencies_path not in sys.path:
            sys.path.append(module_dependencies_path)

        # Prepare viewModel and context
        vm = ViewModel()
        module_context = QQmlContext(window.context)
        module_context.setContextProperty(
            "resultListModel", vm.result_list_model_list)
        module_context.setContextProperty(
            "resultListModelNormalEntries", len(vm.filtered_entry_list))
        module_context.setContextProperty(
            "resultListModelCommandEntries", len(vm.filtered_command_list))
        module_context.setContextProperty(
            "resultListModelHasEntries", False)
        module_context.setContextProperty(
            "resultListModelCommandMode", False)
        module_context.setContextProperty(
            "resultListModelDepth", 0)
        module_context.setContextProperty(
            "contextMenuModel", vm.context_menu_model_list)
        module_context.setContextProperty(
            "contextMenuEnabled", False)

        # Prepare module
        try:
            module_import = __import__(module_dir, fromlist=['Module'])
        except ImportError as e1:
            Logger._log_error(
                "Failed to load module {} from {}: {}".format(module_name, module_dir, e1),
                self.logger)

            # Remove module dependencies path
            sys.path.remove(module_dependencies_path)

            return False

        Module = getattr(module_import, 'Module')

        # Ensure the module implements the base
        assert issubclass(Module, ModuleBase)

        # Set up a queue so that the module can communicate with the main
        # thread
        q = Queue()  # type: Queue

        # Load module
        try:
            module_code = Module()
        except TypeError as e2:
            Logger._log_error(
                "Failed to load module {} from {}: {}".format(module_name, module_dir, e2),
                self.logger)
            return False

        # Check if the required functions have enough parameters
        required_param_lengths = {}

        for name, value in getmembers(ModuleBase, isfunction):
            required_param_lengths[name] = len(signature(value).parameters) - 1  # Python is inconsistent with self

        for name, value in getmembers(module_code, ismethod):
            try:
                required_param_length = required_param_lengths[name]
            except KeyError:
                continue

            param_length = len(signature(value).parameters)

            if param_length != required_param_length:
                if name == 'process_response' and param_length == 1:
                    print("WARN: Module {} uses old process_response signature and will not be able to receive an "
                          "identifier if requested".format(module_name))
                else:
                    Logger._log_error(
                        "Failed to load module {} from {}: {} function has {} parameters (excluding self), expected {}"
                        .format(module_name, module_dir, name, param_length, required_param_length),
                        self.logger)
                    return False

        # Prefill API version and locale
        module['settings']['_api_version'] = [0, 7, 0]
        module['settings']['_locale'] = locale

        # Start the module in the background
        module_thread = ModuleThreadInitializer(
            module_name,
            q,
            target=module_code.init,
            args=(module['settings'], q))
        module_thread.start()

        # Add tab
        tab_data = QQmlComponent(window.engine)
        tab_data.loadUrl(
            QUrl.fromLocalFile(os.path.join(AppFile.get_path(), 'qml', 'ModuleData.qml')))
        window.engine.setContextForObject(tab_data, module_context)
        window.tabs.addTab(module_name, tab_data)

        # Store tab/viewModel combination
        # tabData is not used but stored to prevent segfaults caused by
        # Python garbage collecting it
        window.tab_bindings.append({'init': False,
                                    'queue': q,
                                    'vm': vm,
                                    'module': module_code,
                                    'module_context': module_context,
                                    'module_import': module_import,
                                    'module_name': module_name,
                                    'tab_data': tab_data,
                                    'settings': module['settings'],
                                    'entries_processed': 0})

        # Open tab to trigger loading
        QQmlProperty.write(
            window.tabs, "currentIndex", QQmlProperty.read(window.tabs, "count") - 1)

        return True

    def unload_module(self, window: 'Window', tab_id: int) -> None:
        """Unload a module by tab ID."""
        try:
            window.tab_bindings[tab_id]['module'].stop()
        except Exception as e:
            print('WARN: Module {} caused exception {} on unload'
                  .format(window.tab_bindings[tab_id]['module_name'], e))
            traceback.print_exc()

        if QQmlProperty.read(window.tabs, "currentIndex") == tab_id:
            tab_count = QQmlProperty.read(window.tabs, "count")
            if tab_id + 1 < tab_count:
                QQmlProperty.write(window.tabs, "currentIndex", tab_id + 1)
            else:
                QQmlProperty.write(window.tabs, "currentIndex", "0")

        del window.tab_bindings[tab_id]
        window.tabs.removeTab(tab_id)

    def list_modules(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Return a list of modules together with their source."""
        return ObjectManager().list_objects(self.module_dir)

    def reload_module(self, window: 'Window', tab_id: int) -> bool:
        """Reload a module by tab ID."""
        # Get currently active tab
        current_index = QQmlProperty.read(window.tabs, "currentIndex")

        # Get the needed info to load the module
        module_data = window.tab_bindings[tab_id]
        module = {
            'name': module_data['module_name'],
            'settings': module_data['settings']
        }

        # Unload the module
        self.unload_module(window, tab_id)

        # Force a reload to make code changes happen
        reload(module_data['module_import'])

        # Load it into the UI
        if not self.load_module(window, module, module_data['settings']['_locale']):
            return False

        # Get new position
        new_tab_id = len(window.tab_bindings) - 1

        # Move to correct position if there is more than 1 tab
        if new_tab_id > 0:
            window.tabs.moveTab(new_tab_id, tab_id)
            window.tab_bindings.insert(tab_id, window.tab_bindings.pop(new_tab_id))

            # Focus on active tab
            QQmlProperty.write(window.tabs, "currentIndex", str(current_index))

        # Ensure a proper refresh on the UI side
        window.tabs.currentIndexChanged.emit()

        return True

    def install_module(self, url: str, verbose=False, interactive=True) -> bool:
        """Install a module."""
        module_name = url.rstrip("/").split("/")[-1]

        dir_name = ModuleManager.add_prefix(module_name).replace('.', '_')
        module_name = ModuleManager.remove_prefix(module_name)

        if os.path.exists(os.path.join(self.module_dir, dir_name)):
            if verbose:
                Logger._log('✔⇩ {}'.format(module_name), self.logger)

            return False

        if verbose:
            Logger._log('⇩ {} ({})'.format(module_name, url), self.logger)

        try:
            porcelain.clone(UpdateManager.fix_git_url_for_dulwich(url), os.path.join(self.module_dir, dir_name))
        except Exception as e:
            if verbose:
                Logger._log_error('⇩ {}: {}'.format(module_name, e), self.logger)

            traceback.print_exc()

            try:
                rmtree(os.path.join(self.module_dir, dir_name))
            except FileNotFoundError:
                pass

            return False

        if verbose:
            Logger._log('⇩⇩ {}'.format(module_name), self.logger)

        pip_exit_code = self._pip_install(dir_name)
        if pip_exit_code != 0:
            if verbose:
                Logger._log_error('⇩⇩ {}: {}'.format(module_name, pip_exit_code), self.logger)

            try:
                rmtree(os.path.join(self.module_dir, dir_name))
            except FileNotFoundError:
                pass

            try:
                rmtree(os.path.join(self.module_dependencies_dir, dir_name))
            except FileNotFoundError:
                pass

            return False

        if verbose:
            Logger._log('✔⇩⇩ {}'.format(module_name), self.logger)

        return True

    def uninstall_module(self, module_name: str, verbose=False) -> bool:
        """Uninstall a module."""
        dir_name = ModuleManager.add_prefix(module_name)
        module_name = ModuleManager.remove_prefix(module_name)

        if verbose:
            Logger._log('♻ {}'.format(module_name), self.logger)

        try:
            rmtree(os.path.join(self.module_dir, dir_name))
        except FileNotFoundError:
            if verbose:
                Logger._log(
                    '✔♻ {}'.format(module_name),
                    self.logger)

            return False

        try:
            rmtree(os.path.join(self.module_dependencies_dir, dir_name))
        except FileNotFoundError:
            pass

        if verbose:
            Logger._log('✔♻ {}'.format(module_name), self.logger)

        return True

    def update_module(self, module_name: str, verbose=False) -> bool:
        """Update a module."""
        dir_name = ModuleManager.add_prefix(module_name)
        module_name = ModuleManager.remove_prefix(module_name)

        if verbose:
            Logger._log('⇩ {}'.format(module_name), self.logger)

        try:
            if not UpdateManager.update(os.path.join(self.module_dir, dir_name)):
                if verbose:
                    Logger._log('⏩{}'.format(module_name), self.logger)
                return False

        except Exception as e:
            if verbose:
                Logger._log_error(
                    '⇩ {}: {}'.format(module_name, e),
                    self.logger)

            traceback.print_exc()

            return False

        if verbose:
            Logger._log('⇩⇩ {}'.format(module_name), self.logger)

        pip_exit_code = self._pip_install(dir_name)
        if pip_exit_code != 0:
            if verbose:
                Logger._log_error(
                    '⇩⇩ {}: {}'.format(module_name, pip_exit_code),
                    self.logger)

            return False

        if verbose:
            Logger._log('✔⇩⇩ {}'.format(module_name), self.logger)

        return True

    def update_all_modules(self, verbose=False) -> bool:
        """Update all modules."""
        success = True

        for module in self.list_modules().keys():
            if not self.update_module(module, verbose=verbose):
                success = False

        return success


class UpdateManager():
    """Manages scheduling and checking automatic updates."""

    def __init__(self) -> None:
        """Initialize the UpdateManager and store the version info of Pext."""
        try:
            self.version = UpdateManager.get_version(os.path.dirname(AppFile.get_path()))
        except Exception:
            with open(os.path.join(AppFile.get_path(), 'VERSION')) as version_file:
                self.version = version_file.read().strip()

    @staticmethod
    def _path_to_repo(directory: str) -> Repo:
        return Repo(directory)

    def get_core_version(self) -> str:
        """Return the version info of Pext itself."""
        return self.version

    @staticmethod
    def fix_git_url_for_dulwich(url: str) -> str:
        """Append .git to the URL to work around a Dulwich + GitHub issue.

        Dulwich before 0.18.4 sends an user agent GitHub doesn't respond to correctly.
        """
        if url.startswith("https://") and not url.endswith(".git"):
            url += ".git"

        return url

    @staticmethod
    def get_remote_url(directory: str) -> str:
        """Get the url of the given remote for the specified git-managed directory."""
        with UpdateManager._path_to_repo(directory) as repo:
            config = repo.get_config()
            return config.get(("remote".encode(), "origin".encode()), "url".encode()).decode()

    @staticmethod
    def update(directory: str) -> bool:
        """If an update is available, attempt to update the git-managed directory."""
        # Get current commit
        with UpdateManager._path_to_repo(directory) as repo:
            old_commit = repo[repo.head()]

            # Update
            remote_url = UpdateManager.fix_git_url_for_dulwich(UpdateManager.get_remote_url(directory))
            porcelain.pull(repo, remote_url)

            # See if anything was updated
            return old_commit != repo[repo.head()]

    @staticmethod
    def get_version(directory: str) -> Optional[str]:
        """Get the version of the git-managed directory."""
        from git_describe import describe
        return describe(directory)

    @staticmethod
    def get_last_updated(directory: str) -> Optional[datetime]:
        """Return the time of the latest update of the git-managed directory."""
        with UpdateManager._path_to_repo(directory) as repo:
            commit = repo[repo.head()]
            return datetime.fromtimestamp(commit.commit_time)

    def check_core_update(self) -> Optional[str]:
        """Check if there is an update of the core and if so, return the name of the new version."""
        with urlopen('https://pext.hackerchick.me/version/stable') as update_url:
            available_version = update_url.readline().decode("utf-8").strip()

        # Normalize own version
        if self.version.find('+') != -1:
            print("Current version is an untagged development version, can only check for stable updates")
            normalized_version = self.version[:self.version.find('+')]
        elif self.version.find('-') != -1:
            normalized_version = self.version[:self.version.find('-', self.version.find('-') + 1)]
        else:
            normalized_version = self.version

        if parse_version(normalized_version.lstrip('v')) < parse_version(available_version.lstrip('v')):
            return available_version

        return None


class ModuleThreadInitializer(threading.Thread):
    """Initialize a thread for the module."""

    def __init__(self, module_name: str, q: Queue, target=None, args=()) -> None:
        """Initialize the module thread initializer."""
        self.module_name = module_name
        self.queue = q
        threading.Thread.__init__(self, target=target, args=args)

    def run(self) -> None:
        """Start the module's thread.

        The thread will run forever, until an exception is thrown. If an
        exception is thrown, an Action.critical_error is appended to the
        queue.
        """
        try:
            threading.Thread.run(self)
        except Exception as e:
            self.queue.put(
                [Action.critical_error, "Exception thrown: {}".format(e)])
            traceback.print_exc()


class ViewModel():
    """Manage the communication between user interface and module."""

    def __init__(self) -> None:
        """Initialize ViewModel."""
        # Temporary values to allow binding. These will be properly set when
        # possible and relevant.
        self.command_list = []  # type: List
        self.entry_list = []  # type: List
        self.filtered_entry_list = []  # type: List
        self.filtered_command_list = []  # type: List
        self.result_list_model_list = QStringListModel()
        self.result_list_model_max_index = -1
        self.result_list_model_command_mode = False
        self.result_list_model_command_mode_new = True
        self.selection = []  # type: List[Dict[SelectionType, str]]
        self.last_search = ""
        self.context_menu_model_list = QStringListModel()
        self.extra_info_entries = {}  # type: Dict[str, str]
        self.extra_info_commands = {}  # type: Dict[str, str]
        self.context_menu_entries = {}  # type: Dict[str, List[str]]
        self.context_menu_commands = {}  # type: Dict[str, List[str]]
        self.context_menu_base = []  # type: List[str]
        self.context_menu_base_open = False
        self.extra_info_last_entry = ""
        self.extra_info_last_entry_type = None
        self.selection_thread = None  # type: threading.Thread

    def make_selection(self) -> None:
        """Make a selection if no selection is currently being processed.

        Running the selection making in another thread prevents it from locking
        up Pext's UI, while ensuring existing thread completion prevents race
        conditions.
        """
        if self.selection_thread and self.selection_thread.is_alive():
            return

        self.selection_thread = threading.Thread(target=self.module.selection_made, args=(self.selection,))
        self.selection_thread.start()

    def _get_longest_common_string(self, entries: List[str], start="") -> Optional[str]:
        """Return the longest common string.

        Returns the longest common string for each entry in the list, starting
        at the start.

        Keyword arguments:
        entries -- the list of entries
        start -- the string to start with (default "")

        All entries not starting with start are discarded before additional
        matches are looked for.

        Returns the longest common string, or None if not a single value
        matches start.
        """
        # Filter out all entries that don't match at the start
        entry_list = []
        for entry in entries:
            if entry.startswith(start):
                entry_list.append(entry)

        common_chars = list(start)

        try:
            while True:
                common_char = None
                for entry in entry_list:
                    if common_char is None:
                        common_char = entry[len(common_chars)]
                    elif common_char != entry[len(common_chars)]:
                        return ''.join(common_chars)

                if common_char is None:
                    return None

                common_chars.append(common_char)
        except IndexError:
            # We fully match a string
            return ''.join(common_chars)

    def _clear_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                return
            self.queue.task_done()

    def bind_context(self, queue: Queue, context: QQmlContext, window: 'Window', search_input_model: QObject,
                     header_text: QObject, result_list_model: QObject, context_menu_model: QObject,
                     base_info_panel: QObject, context_info_panel: QObject) -> None:
        """Bind the QML context so we can communicate with the QML front-end."""
        self.queue = queue
        self.context = context
        self.window = window
        self.search_input_model = search_input_model
        self.header_text = header_text
        self.result_list_model = result_list_model
        self.context_menu_model = context_menu_model
        self.base_info_panel = base_info_panel
        self.context_info_panel = context_info_panel

    def bind_module(self, module: ModuleBase) -> None:
        """Bind the module.

        This ensures we can call functions in it.
        """
        self.module = module

    def go_up(self) -> None:
        """Go one level up.

        This means that, if we're currently in the entry content list, we go
        back to the entry list. If we're currently in the entry list, we clear
        the search bar. If we're currently in the entry list and the search bar
        is empty, we tell the window to hide/close itself.
        """
        if self.context.contextProperty("contextMenuEnabled"):
            self.context_menu_base_open = False
            self.context.setContextProperty(
                "contextMenuEnabled", False)
            return

        if QQmlProperty.read(self.search_input_model, "text") != "":
            QQmlProperty.write(self.search_input_model, "text", "")
            return

        if self.selection_thread and self.selection_thread.is_alive():
            return

        if len(self.selection) > 0:
            self.selection.pop()
            self.entry_list = []
            self.command_list = []

            self.search(new_entries=True)

            self.context.setContextProperty(
                "resultListModelDepth", len(self.selection))

            self._clear_queue()
            self.window.update()

            self.make_selection()
        else:
            self.window.close(manual=True)

    def search(self, new_entries=False, manual=False) -> None:
        """Filter the entry list.

        Filter the list of entries in the screen, setting the filtered list
        to the entries containing one or more words of the string currently
        visible in the search bar.
        """
        search_string = QQmlProperty.read(self.search_input_model, "text").lower()

        # Don't search if nothing changed
        if not new_entries and search_string == self.last_search:
            return

        # TODO: Enable searching in context menu
        if manual:
            self.context_menu_base_open = False
            self.context.setContextProperty(
                "contextMenuEnabled", False)

        # Sort if sorting is enabled
        if Settings.get('sort_mode') != SortMode.Module:
            reverse = Settings.get('sort_mode') == SortMode.Descending
            self.sorted_entry_list = sorted(self.entry_list, reverse=reverse)
            self.sorted_command_list = sorted(self.command_list, reverse=reverse)
            self.sorted_filtered_entry_list = sorted(self.filtered_entry_list, reverse=reverse)
            self.sorted_filtered_command_list = sorted(self.filtered_command_list, reverse=reverse)
        else:
            self.sorted_entry_list = self.entry_list
            self.sorted_command_list = self.command_list
            self.sorted_filtered_entry_list = self.filtered_entry_list
            self.sorted_filtered_command_list = self.filtered_command_list

        # Get current match
        try:
            current_match = self.result_list_model_list.stringList()[QQmlProperty.read(self.result_list_model,
                                                                                       "currentIndex")]
        except IndexError:
            current_match = None

        # If empty, show all
        if len(search_string) == 0 and not new_entries:
            self.filtered_entry_list = self.entry_list
            self.filtered_command_list = self.command_list
            self.sorted_filtered_entry_list = self.sorted_entry_list
            self.sorted_filtered_command_list = self.sorted_command_list

            combined_list = self.sorted_filtered_entry_list + self.sorted_filtered_command_list

            self.result_list_model_list.setStringList(str(entry) for entry in combined_list)

            self.context.setContextProperty(
                "resultListModelNormalEntries", len(self.sorted_filtered_entry_list))
            self.context.setContextProperty(
                "resultListModelCommandEntries", len(self.sorted_filtered_command_list))

            self.context.setContextProperty(
                "resultListModelCommandMode", False)

            # Keep existing selection, otherwise ensure something is selected
            try:
                current_index = combined_list.index(current_match)
            except ValueError:
                current_index = 0

            QQmlProperty.write(self.result_list_model, "currentIndex", current_index)

            # Enable checking for changes next time
            self.last_search = search_string

            self.update_context_info_panel()

            return

        search_strings = search_string.split(" ")

        # If longer and no new entries, only filter existing list
        if (len(self.last_search) > 0 and len(search_string) > len(self.last_search)
                and not self.result_list_model_command_mode):

            filter_entry_list = self.sorted_filtered_entry_list
            filter_command_list = self.sorted_filtered_command_list
        else:
            filter_entry_list = self.sorted_entry_list
            filter_command_list = self.sorted_command_list

        self.filtered_entry_list = []
        self.filtered_command_list = []

        activate_command_mode = False

        for command in filter_command_list:
            if search_strings[0] in command:
                if search_strings[0] == command.split(" ", 1)[0] and len(search_string) >= len(self.last_search):
                    activate_command_mode = True
                    if manual and self.result_list_model_command_mode:
                        self.result_list_model_command_mode_new = False
                    self.result_list_model_command_mode = True

                self.filtered_command_list.append(command)

        if not activate_command_mode:
            self.result_list_model_command_mode = False
            self.result_list_model_command_mode_new = True

        if self.result_list_model_command_mode:
            for entry in filter_entry_list:
                if all(search_string in str(entry).lower() for search_string in search_strings[1:]):
                    self.filtered_entry_list.append(entry)

            combined_list = self.filtered_command_list + self.filtered_entry_list
        else:
            for entry in filter_entry_list:
                if all(search_string in str(entry).lower() for search_string in search_strings):
                    self.filtered_entry_list.append(entry)

            combined_list = self.filtered_entry_list + self.filtered_command_list

        self.context.setContextProperty(
            "resultListModelCommandMode", self.result_list_model_command_mode)

        self.context.setContextProperty(
            "resultListModelNormalEntries", len(self.filtered_entry_list))
        self.context.setContextProperty(
            "resultListModelCommandEntries", len(self.filtered_command_list))

        self.result_list_model_list.setStringList(str(entry) for entry in combined_list)

        # Keep existing selection, otherwise ensure something is selected
        if manual and self.result_list_model_command_mode and self.result_list_model_command_mode_new:
            current_index = 0
        else:
            try:
                current_index = combined_list.index(current_match)
            except ValueError:
                current_index = 0

        QQmlProperty.write(self.result_list_model, "currentIndex", current_index)

        # Enable checking for changes next time
        self.last_search = search_string

        self.update_context_info_panel()

    def _get_entry(self, include_context=False, shorten_command=False) -> Dict:
        """Get info on the entry that's currently focused."""
        if include_context and self.context.contextProperty("contextMenuEnabled"):
            current_index = QQmlProperty.read(self.context_menu_model, "currentIndex")

            selected_entry = self._get_entry(shorten_command=shorten_command)

            selected_entry['context_option'] = self.context_menu_model_list.stringList()[current_index]

            return selected_entry

        if self.context.contextProperty("contextMenuEnabled") and self.context_menu_base_open:
            return {'type': SelectionType.none, 'value': None, 'context_option': None}

        current_index = QQmlProperty.read(self.result_list_model, "currentIndex")

        if self.result_list_model_command_mode:
            try:
                selected_command = self.filtered_command_list[current_index]
            except IndexError:
                entry = self.filtered_entry_list[current_index - len(self.filtered_command_list)]
                return {'type': SelectionType.entry, 'value': entry, 'context_option': None}

            selected_command_split = selected_command.split(" ", 1)
            command_typed = QQmlProperty.read(self.search_input_model, "text")
            command_typed_split = command_typed.split(" ", 1)

            if shorten_command:
                command_typed = selected_command_split[0]
            else:
                try:
                    command_typed = selected_command_split[0] + " " + command_typed_split[1]
                except IndexError:
                    command_typed = selected_command_split[0]

            return {'type': SelectionType.command, 'value': command_typed, 'context_option': None}

        if current_index >= len(self.filtered_entry_list):
            entry = self.filtered_command_list[current_index - len(self.filtered_entry_list)]
            return {'type': SelectionType.command, 'value': entry, 'context_option': None}
        else:
            entry = self.filtered_entry_list[current_index]
            return {'type': SelectionType.entry, 'value': entry, 'context_option': None}

    def select(self) -> None:
        """Notify the module of our selection entry."""
        if len(self.filtered_entry_list + self.filtered_command_list) == 0:
            return

        if self.selection_thread and self.selection_thread.is_alive():
            return

        self.entry_list = []
        self.command_list = []
        self.extra_info_entries = {}
        self.extra_info_commands = {}
        self.context_menu_entries = {}
        self.context_menu_commands = {}

        self.selection.append(self._get_entry(include_context=True))

        self.context.setContextProperty(
            "contextMenuEnabled", False)
        self.context.setContextProperty(
            "resultListModelDepth", len(self.selection))

        QQmlProperty.write(self.search_input_model, "text", "")
        self.search(new_entries=True, manual=True)
        self._clear_queue()
        self.window.update()

        self.make_selection()

    def show_context_base(self) -> None:
        """Show the base context menu."""
        if not QQmlProperty.read(self.header_text, "text"):
            return

        self.context_menu_base_open = True

        self.context_menu_model_list.setStringList(str(entry) for entry in self.context_menu_base)
        self.context.setContextProperty(
            "contextMenuEnabled", True)

    def show_context(self) -> None:
        """Show the context menu of the selected entry."""
        if len(self.filtered_entry_list + self.filtered_command_list) == 0:
            return

        current_entry = self._get_entry(shorten_command=True)

        try:
            if current_entry['type'] == SelectionType.entry:
                self.context_menu_model_list.setStringList(
                    str(entry) for entry in self.context_menu_entries[current_entry['value']])
            else:
                self.context_menu_model_list.setStringList(
                    str(entry) for entry in self.context_menu_commands[current_entry['value']])

            self.context_menu_base_open = False
            self.context.setContextProperty(
                "contextMenuEnabled", True)
        except KeyError:
            pass  # No menu available, do nothing

    def hide_context(self) -> None:
        """Hide the context menu."""
        self.context_menu_base_open = False
        self.context.setContextProperty(
            "contextMenuEnabled", False)

    def update_context_info_panel(self, request_update=True) -> None:
        """Update the context info panel with the info panel data of the currently selected entry."""
        if len(self.filtered_entry_list + self.filtered_command_list) == 0:
            QQmlProperty.write(self.context_info_panel, "text", "")
            self.extra_info_last_entry_type = None
            return

        current_entry = self._get_entry(shorten_command=True)

        # Prevent updating the list unnecessarily often
        if (current_entry['value'] == self.extra_info_last_entry
                and current_entry['type'] == self.extra_info_last_entry_type):
            return

        self.extra_info_last_entry = current_entry['value']
        self.extra_info_last_entry_type = current_entry['type']

        if request_update:
            info_selection = self.selection[:]
            new_selection_entry = current_entry
            info_selection.append(new_selection_entry)

            threading.Thread(target=self.module.extra_info_request, args=(info_selection,)).start()

        try:
            if current_entry['type'] == SelectionType.entry:
                QQmlProperty.write(self.context_info_panel, "text", self.extra_info_entries[current_entry['value']])
            else:
                QQmlProperty.write(self.context_info_panel, "text", self.extra_info_commands[current_entry['value']])
        except KeyError:
            QQmlProperty.write(self.context_info_panel, "text", "")

    def update_base_info_panel(self, base_info: str) -> None:
        """Update the base info panel based on the current module state."""
        QQmlProperty.write(self.base_info_panel, "text", str(base_info))

    def set_header(self, content) -> None:
        """Set the header text."""
        QQmlProperty.write(self.header_text, "text", str(content))

    def tab_complete(self) -> None:
        """Tab-complete based on the current seach input.

        This tab-completes the command, entry or combination currently in the
        search bar to the longest possible common completion.
        """
        current_input = QQmlProperty.read(self.search_input_model, "text")

        start = current_input

        possibles = current_input.split(" ", 1)
        command = self._get_longest_common_string(
            [command.split(" ", 1)[0] for command in self.command_list],
            start=possibles[0])
        # If we didn't complete the command, see if we can complete the text
        if command is None or len(command) == len(possibles[0]):
            if command is None:
                command = ""  # We string concat this later
            else:
                command += " "

            start = possibles[1] if len(possibles) > 1 else ""
            entry = self._get_longest_common_string([list_entry for list_entry in self.filtered_entry_list
                                                     if list_entry not in self.command_list],
                                                    start=start)

            if entry is None or len(entry) <= len(start):
                self.queue.put(
                    [Action.add_error, "No tab completion possible"])
                return
        else:
            entry = " "  # Add an extra space to simplify typing for the user

        QQmlProperty.write(self.search_input_model, "text", command + entry)
        self.search()


class Window(QMainWindow):
    """The main Pext window."""

    def __init__(self, config_retriever: ConfigRetriever, parent=None) -> None:
        """Initialize the window."""
        super().__init__(parent)

        # Save settings
        self.config_retriever = config_retriever

        self.tab_bindings = []  # type: List[Dict]

        self.engine = QQmlApplicationEngine(self)

        # Set QML variables
        self.context = self.engine.rootContext()
        self.context.setContextProperty(
            "applicationVersion", UpdateManager().get_core_version())
        self.context.setContextProperty(
            "systemPlatform", platform.system())

        self.context.setContextProperty(
            "modulesPath", os.path.join(self.config_retriever.get_setting('config_path'), 'modules'))
        self.context.setContextProperty(
            "themesPath", os.path.join(self.config_retriever.get_setting('config_path'), 'themes'))

        self.context.setContextProperty("currentTheme", Settings.get('theme'))
        self.context.setContextProperty("currentProfile", Settings.get('profile'))

        # Load the main UI
        self.engine.load(QUrl.fromLocalFile(os.path.join(AppFile.get_path(), 'qml', 'main.qml')))

        self.window = self.engine.rootObjects()[0]

        # Override quit and minimize
        self.window.closing.connect(self.quit)
        self.window.windowStateChanged.connect(self._process_window_state)

        # Give QML the module info
        self.intro_screen = self.window.findChild(QObject, "introScreen")
        self.module_manager = ModuleManager(self.config_retriever)
        self._update_modules_info_qml()

        # Give QML the theme info
        self.theme_manager = ThemeManager(self.config_retriever)
        self._update_themes_info_qml()

        # Give QML the profile info
        self.profile_manager = ProfileManager(self.config_retriever)
        self._update_profiles_info_qml()

        # Bind update dialog
        self.update_available_requests = self.window.findChild(QObject, "updateAvailableRequests")
        self.update_available_requests.updateAvailableDialogAccepted.connect(self._show_download_page)

        # Bind global shortcuts
        self.search_input_model = self.window.findChild(
            QObject, "searchInputModel")
        escape_shortcut = self.window.findChild(QObject, "escapeShortcut")
        back_button = self.window.findChild(QObject, "backButton")
        tab_shortcut = self.window.findChild(QObject, "tabShortcut")

        self.search_input_model.textChanged.connect(self._search)
        self.search_input_model.accepted.connect(self._select)
        escape_shortcut.activated.connect(self._go_up)
        back_button.clicked.connect(self._go_up)
        tab_shortcut.activated.connect(self._tab_complete)

        # Find menu entries
        menu_reload_active_module_shortcut = self.window.findChild(
            QObject, "menuReloadActiveModule")
        menu_load_module_shortcut = self.window.findChild(
            QObject, "menuLoadModule")
        menu_close_active_module_shortcut = self.window.findChild(
            QObject, "menuCloseActiveModule")
        menu_install_module_shortcut = self.window.findChild(
            QObject, "menuInstallModule")
        menu_manage_modules_shortcut = self.window.findChild(
            QObject, "menuManageModules")
        menu_update_all_modules_shortcut = self.window.findChild(
            QObject, "menuUpdateAllModules")

        menu_load_theme_shortcut = self.window.findChild(
            QObject, "menuLoadTheme")
        menu_install_theme_shortcut = self.window.findChild(
            QObject, "menuInstallTheme")
        menu_manage_themes_shortcut = self.window.findChild(
            QObject, "menuManageThemes")
        menu_update_all_themes_shortcut = self.window.findChild(
            QObject, "menuUpdateAllThemes")

        menu_load_profile_shortcut = self.window.findChild(
            QObject, "menuLoadProfile")
        menu_manage_profiles_shortcut = self.window.findChild(
            QObject, "menuManageProfiles")

        menu_sort_module_shortcut = self.window.findChild(
            QObject, "menuSortModule")
        menu_sort_ascending_shortcut = self.window.findChild(
            QObject, "menuSortAscending")
        menu_sort_descending_shortcut = self.window.findChild(
            QObject, "menuSortDescending")

        menu_minimize_normally_shortcut = self.window.findChild(
            QObject, "menuMinimizeNormally")
        menu_minimize_to_tray_shortcut = self.window.findChild(
            QObject, "menuMinimizeToTray")
        menu_minimize_normally_manually_shortcut = self.window.findChild(
            QObject, "menuMinimizeNormallyManually")
        menu_minimize_to_tray_manually_shortcut = self.window.findChild(
            QObject, "menuMinimizeToTrayManually")
        menu_show_tray_icon_shortcut = self.window.findChild(
            QObject, "menuShowTrayIcon")
        self.menu_enable_update_check_shortcut = self.window.findChild(
            QObject, "menuEnableUpdateCheck")

        menu_quit_shortcut = self.window.findChild(QObject, "menuQuit")
        menu_quit_without_saving_shortcut = self.window.findChild(
            QObject, "menuQuitWithoutSaving")
        menu_check_for_updates_shortcut = self.window.findChild(QObject, "menuCheckForUpdates")
        menu_homepage_shortcut = self.window.findChild(QObject, "menuHomepage")

        # Bind menu entries
        menu_reload_active_module_shortcut.triggered.connect(
            self._reload_active_module)
        menu_load_module_shortcut.loadModuleRequest.connect(self._open_tab)
        menu_close_active_module_shortcut.triggered.connect(self._close_tab)
        menu_install_module_shortcut.installModuleRequest.connect(
            self._menu_install_module)
        menu_manage_modules_shortcut.uninstallModuleRequest.connect(
            self._menu_uninstall_module)
        menu_manage_modules_shortcut.updateModuleRequest.connect(self._menu_update_module)
        menu_update_all_modules_shortcut.updateAllModulesRequest.connect(
            self._menu_update_all_modules)

        menu_load_theme_shortcut.loadThemeRequest.connect(self._menu_switch_theme)
        menu_install_theme_shortcut.installThemeRequest.connect(
            self._menu_install_theme)
        menu_manage_themes_shortcut.uninstallThemeRequest.connect(
            self._menu_uninstall_theme)
        menu_manage_themes_shortcut.updateThemeRequest.connect(self._menu_update_theme)
        menu_update_all_themes_shortcut.updateAllThemesRequest.connect(
            self._menu_update_all_themes)

        menu_load_profile_shortcut.loadProfileRequest.connect(self._menu_switch_profile)
        menu_manage_profiles_shortcut.createProfileRequest.connect(self._menu_create_profile)
        menu_manage_profiles_shortcut.removeProfileRequest.connect(self._menu_remove_profile)

        menu_sort_module_shortcut.toggled.connect(self._menu_sort_module)
        menu_sort_ascending_shortcut.toggled.connect(self._menu_sort_ascending)
        menu_sort_descending_shortcut.toggled.connect(self._menu_sort_descending)

        menu_minimize_normally_shortcut.toggled.connect(self._menu_minimize_normally)
        menu_minimize_to_tray_shortcut.toggled.connect(self._menu_minimize_to_tray)
        menu_minimize_normally_manually_shortcut.toggled.connect(self._menu_minimize_normally_manually)
        menu_minimize_to_tray_manually_shortcut.toggled.connect(self._menu_minimize_to_tray_manually)
        menu_show_tray_icon_shortcut.toggled.connect(self._menu_toggle_tray_icon)
        self.menu_enable_update_check_shortcut.toggled.connect(self._menu_toggle_update_check)

        menu_quit_shortcut.triggered.connect(self.quit)
        menu_quit_without_saving_shortcut.triggered.connect(
            self.quit_without_saving)
        menu_check_for_updates_shortcut.triggered.connect(self._menu_check_updates)
        menu_homepage_shortcut.triggered.connect(self._show_homepage)

        # Set entry states
        QQmlProperty.write(menu_sort_module_shortcut,
                           "checked",
                           int(Settings.get('sort_mode')) == SortMode.Module)
        QQmlProperty.write(menu_sort_ascending_shortcut,
                           "checked",
                           int(Settings.get('sort_mode')) == SortMode.Ascending)
        QQmlProperty.write(menu_sort_descending_shortcut,
                           "checked",
                           int(Settings.get('sort_mode')) == SortMode.Descending)

        QQmlProperty.write(menu_minimize_normally_shortcut,
                           "checked",
                           int(Settings.get('minimize_mode')) == MinimizeMode.Normal)
        QQmlProperty.write(menu_minimize_to_tray_shortcut,
                           "checked",
                           int(Settings.get('minimize_mode')) == MinimizeMode.Tray)
        QQmlProperty.write(menu_minimize_normally_manually_shortcut,
                           "checked",
                           int(Settings.get('minimize_mode')) == MinimizeMode.NormalManualOnly)
        QQmlProperty.write(menu_minimize_to_tray_manually_shortcut,
                           "checked",
                           int(Settings.get('minimize_mode')) == MinimizeMode.TrayManualOnly)

        QQmlProperty.write(menu_show_tray_icon_shortcut,
                           "checked",
                           Settings.get('tray'))
        QQmlProperty.write(self.menu_enable_update_check_shortcut,
                           "checked",
                           Settings.get('update_check'))

        # Get reference to tabs list
        self.tabs = self.window.findChild(QObject, "tabs")

        # Bind the context when the tab is loaded
        self.tabs.currentIndexChanged.connect(self._bind_context)

        # Show the window if not --background
        if not Settings.get('background'):
            self.show()

            if Settings.get('update_check') is None:
                # Ask if the user wants to enable automatic update checking
                permission_requests = self.window.findChild(QObject, "permissionRequests")

                permission_requests.updatePermissionRequestAccepted.connect(
                    lambda: self._menu_toggle_update_check(True, after_permission_request=True))
                permission_requests.updatePermissionRequestRejected.connect(
                    lambda: self._menu_toggle_update_check(False))

                permission_requests.updatePermissionRequest.emit()

    def _bind_context(self) -> None:
        """Bind the context for the module."""
        current_tab = QQmlProperty.read(self.tabs, "currentIndex")
        element = self.tab_bindings[current_tab]

        # Only initialize once, ensure filter is applied
        if element['init']:
            element['vm'].search(new_entries=True)
            return

        # Get the header
        header_text = self.tabs.getTab(
            current_tab).findChild(QObject, "headerText")

        # Get the list
        result_list_model = self.tabs.getTab(
            current_tab).findChild(QObject, "resultListModel")

        # Get the info panels
        base_info_panel = self.tabs.getTab(
            current_tab).findChild(QObject, "baseInfoPanel")
        context_info_panel = self.tabs.getTab(
            current_tab).findChild(QObject, "contextInfoPanel")

        # Get the context menu
        context_menu_model = self.tabs.getTab(
            current_tab).findChild(QObject, "contextMenuModel")

        # Enable mouse selection support
        result_list_model.entryClicked.connect(element['vm'].select)
        result_list_model.openBaseMenu.connect(element['vm'].show_context_base)
        result_list_model.openContextMenu.connect(element['vm'].show_context)
        context_menu_model.entryClicked.connect(element['vm'].select)
        context_menu_model.closeContextMenu.connect(element['vm'].hide_context)

        # Enable info pane
        result_list_model.currentIndexChanged.connect(element['vm'].update_context_info_panel)

        # Bind it to the viewmodel
        element['vm'].bind_context(element['queue'],
                                   element['module_context'],
                                   self,
                                   self.search_input_model,
                                   header_text,
                                   result_list_model,
                                   context_menu_model,
                                   base_info_panel,
                                   context_info_panel)

        element['vm'].bind_module(element['module'])

        # Done initializing
        element['init'] = True

    def _process_window_state(self, event) -> None:
        if event & Qt.WindowMinimized:
            if Settings.get('minimize_mode') in [MinimizeMode.Tray, MinimizeMode.TrayManualOnly]:
                self.window.hide()

    def _get_current_element(self) -> Optional[Dict]:
        current_tab = QQmlProperty.read(self.tabs, "currentIndex")
        try:
            return self.tab_bindings[current_tab]
        except IndexError:
            # No tabs
            return None

    def _go_up(self) -> None:
        try:
            self._get_current_element()['vm'].go_up()
        except TypeError:
            pass

    def _open_tab(self, name: str, settings: str) -> None:
        module_settings = {}
        for setting in settings.split(" "):
            try:
                key, value = setting.split("=", 2)
            except ValueError:
                continue

            module_settings[key] = value

        module = {'name': name, 'settings': module_settings}
        self.module_manager.load_module(self, module, Settings.get('locale'))
        # First module? Enforce load
        if len(self.tab_bindings) == 1:
            self.tabs.currentIndexChanged.emit()

    def _close_tab(self) -> None:
        if len(self.tab_bindings) > 0:
            self.module_manager.unload_module(
                self,
                QQmlProperty.read(self.tabs, "currentIndex"))

    def _reload_active_module(self) -> None:
        if len(self.tab_bindings) > 0:
            self.module_manager.reload_module(
                self,
                QQmlProperty.read(self.tabs, "currentIndex"))

    def _menu_install_module(self, module_url: str) -> None:
        functions = [
            {
                'name': self.module_manager.install_module,
                'args': (module_url),
                'kwargs': {'interactive': False, 'verbose': True}
            }, {
                'name': self._update_modules_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_uninstall_module(self, module_name: str) -> None:
        functions = [
            {
                'name': self.module_manager.uninstall_module,
                'args': (module_name),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_modules_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_update_module(self, module_name: str) -> None:
        functions = [
            {
                'name': self.module_manager.update_module,
                'args': (module_name),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_modules_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_update_all_modules(self) -> None:
        functions = [
            {
                'name': self.module_manager.update_all_modules,
                'args': (),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_modules_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_restart_pext(self, extra_args=[]) -> None:
        # Call _shut_down manually because it isn't called when using os.execv
        _shut_down(self,
                   self.config_retriever)

        args = sys.argv[:]
        args.extend(extra_args)

        args.insert(0, sys.executable)
        if sys.platform == 'win32':
            args = ['"%s"' % arg for arg in args]

        os.chdir(os.getcwd())
        os.execv(sys.executable, args)

    def _menu_switch_theme(self, theme_name: Optional[str]) -> None:
        Settings.set('theme', theme_name)

        self._menu_restart_pext()

    def _menu_switch_profile(self, profile_name: str, new_instance=bool) -> None:
        extra_args = ['--profile={}'.format(profile_name)]
        if not new_instance:
            self._menu_restart_pext(extra_args)
        else:
            args = sys.argv[:]
            args.extend(extra_args)
            args.insert(0, sys.executable)

            Popen(args)

    def _menu_create_profile(self, profile_name: str) -> None:
        functions = [
            {
                'name': self.profile_manager.create_profile,
                'args': (profile_name),
                'kwargs': {}
            }, {
                'name': self._update_profiles_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_remove_profile(self, profile_name: str) -> None:
        functions = [
            {
                'name': self.profile_manager.remove_profile,
                'args': (profile_name),
                'kwargs': {}
            }, {
                'name': self._update_profiles_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_install_theme(self, theme_url: str) -> None:
        functions = [
            {
                'name': self.theme_manager.install_theme,
                'args': (theme_url),
                'kwargs': {'interactive': False, 'verbose': True}
            }, {
                'name': self._update_themes_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_uninstall_theme(self, theme_name: str) -> None:
        functions = [
            {
                'name': self.theme_manager.uninstall_theme,
                'args': (theme_name),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_themes_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_update_theme(self, theme_name: str) -> None:
        functions = [
            {
                'name': self.theme_manager.update_theme,
                'args': (theme_name),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_themes_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_update_all_themes(self) -> None:
        functions = [
            {
                'name': self.theme_manager.update_all_themes,
                'args': (),
                'kwargs': {'verbose': True}
            }, {
                'name': self._update_themes_info_qml,
                'args': (),
                'kwargs': {}
            }
        ]
        threading.Thread(target=RunConseq, args=(functions,)).start()  # type: ignore

    def _menu_sort_module(self, enabled: bool) -> None:
        if enabled:
            Settings.set('sort_mode', SortMode.Module)
            for tab in self.tab_bindings:
                tab['vm'].search(new_entries=True)

    def _menu_sort_ascending(self, enabled: bool) -> None:
        if enabled:
            Settings.set('sort_mode', SortMode.Ascending)
            for tab in self.tab_bindings:
                tab['vm'].search(new_entries=True)

    def _menu_sort_descending(self, enabled: bool) -> None:
        if enabled:
            Settings.set('sort_mode', SortMode.Descending)
            for tab in self.tab_bindings:
                tab['vm'].search(new_entries=True)

    def _menu_minimize_normally(self, enabled: bool) -> None:
        if enabled:
            Settings.set('minimize_mode', MinimizeMode.Normal)

    def _menu_minimize_to_tray(self, enabled: bool) -> None:
        if enabled:
            Settings.set('minimize_mode', MinimizeMode.Tray)

    def _menu_minimize_normally_manually(self, enabled: bool) -> None:
        if enabled:
            Settings.set('minimize_mode', MinimizeMode.NormalManualOnly)

    def _menu_minimize_to_tray_manually(self, enabled: bool) -> None:
        if enabled:
            Settings.set('minimize_mode', MinimizeMode.TrayManualOnly)

    def _menu_toggle_tray_icon(self, enabled: bool) -> None:
        Settings.set('tray', enabled)
        try:
            self.tray.show() if enabled else self.tray.hide()  # type: ignore
        except AttributeError:
            pass

    def _menu_toggle_update_check(self, enabled: bool, after_permission_request=False) -> None:
        Settings.set('update_check', enabled)
        QQmlProperty.write(self.menu_enable_update_check_shortcut,
                           "checked",
                           Settings.get('update_check'))

        # Immediately save update status to file
        self.config_retriever.save_updatecheck_permission(Settings.get('update_check'))

        # macOS breaks if we show the update dialog immediately after accepting
        # checking for updates so we need this workaround
        if after_permission_request:
            self._menu_restart_pext()

        # Check for updates immediately after toggling true
        # This is also toggled on app launch because we bind before we toggle
        if Settings.get('update_check'):
            self._menu_check_updates(verbose=False, manual=False)

    def _search(self) -> None:
        try:
            self._get_current_element()['vm'].search(manual=True)
        except TypeError:
            pass

    def _select(self) -> None:
        try:
            self._get_current_element()['vm'].select()
        except TypeError:
            pass

    def _tab_complete(self) -> None:
        try:
            self._get_current_element()['vm'].tab_complete()
        except TypeError:
            pass

    def _update_modules_info_qml(self) -> None:
        modules = self.module_manager.list_modules()
        self.context.setContextProperty(
            "modules", modules)
        QQmlProperty.write(
            self.intro_screen, "modulesInstalledCount", len(modules.keys()))

    def _update_themes_info_qml(self) -> None:
        themes = self.theme_manager.list_themes()
        self.context.setContextProperty(
            "themes", themes)

    def _update_profiles_info_qml(self) -> None:
        profiles = self.profile_manager.list_profiles()
        self.context.setContextProperty(
            "profiles", profiles)

    def _menu_check_updates_actually_check(self, verbose=True) -> None:
        if verbose:
            Logger._log('⇩ Pext', self.logger)

        try:
            new_version = UpdateManager().check_core_update()
        except Exception as e:
            Logger._log_error('⇩ Pext: {}'.format(e), self.logger)
            traceback.print_exc()
            return

        if new_version:
            # Show update dialog (already bound at initialization)
            self.update_available_requests.showUpdateAvailableDialog.emit()
        else:
            if verbose:
                Logger._log('✔⇩ Pext', self.logger)

    def _menu_check_updates(self, verbose=True, manual=True) -> None:
        # Set a timer to run this function again in an hour
        if not manual:
            t = threading.Timer(3600, self._menu_check_updates, None, {'verbose': False, 'manual': False})
            t.daemon = True
            t.start()

        # Check if it's been over 24 hours or this is a manual check
        last_update_check = self.config_retriever.get_last_update_check_time()
        if manual or last_update_check is None or (datetime.now() - last_update_check).total_seconds() > (86400):
            threading.Thread(target=self._menu_check_updates_actually_check, args=(verbose,)).start()
            self.config_retriever.set_last_update_check_time(datetime.now())

    def _show_homepage(self) -> None:
        webbrowser.open('https://pext.hackerchick.me/')

    def _show_download_page(self) -> None:
        webbrowser.open('https://pext.hackerchick.me/download')

    def bind_logger(self, logger: 'Logger') -> None:
        """Bind the logger to the window and further initialize the module."""
        self.logger = logger

        self.module_manager.bind_logger(logger)
        self.theme_manager.bind_logger(logger)

        # Now that the logger is bound, we can show messages in the window, so
        # start binding the modules
        if len(Settings.get('modules')) > 0:
            for module in Settings.get('modules'):
                self.module_manager.load_module(self, module, Settings.get('locale'))
        else:
            for module in ProfileManager(self.config_retriever).retrieve_modules(Settings.get('profile')):
                self.module_manager.load_module(self, module, Settings.get('locale'))

        # If there's only one module passed through the command line, enforce
        # loading it now. Otherwise, switch back to the first module in the
        # list
        if len(self.tab_bindings) == 1:
            self.tabs.currentIndexChanged.emit()
        elif len(self.tab_bindings) > 1:
            QQmlProperty.write(self.tabs, "currentIndex", "0")

    def bind_tray(self, tray: 'Tray') -> None:
        """Bind the tray to the window."""
        self.tray = tray

        if Settings.get('tray'):
            tray.show()

    def close(self, manual=False, force_tray=False) -> None:
        """Close the window."""
        if force_tray:
            self.window.hide()
        elif (Settings.get('minimize_mode') == MinimizeMode.Normal
                or (manual and Settings.get('minimize_mode') == MinimizeMode.NormalManualOnly)):
            self.window.showMinimized()
        elif (Settings.get('minimize_mode') == MinimizeMode.Tray
                or (manual and Settings.get('minimize_mode') == MinimizeMode.TrayManualOnly)):
            self.window.hide()

    def show(self) -> None:
        """Show the window."""
        if self.window.windowState() == Qt.WindowMinimized:
            self.window.showNormal()
        else:
            self.window.show()

        self.window.raise_()
        self.activateWindow()

    def toggle_visibility(self, force_tray=False) -> None:
        """Toggle window visibility."""
        if self.window.windowState() == Qt.WindowMinimized or not self.window.isVisible():
            self.show()
        else:
            self.close(force_tray=force_tray)

    def quit(self) -> None:
        """Quit."""
        sys.exit(0)

    def quit_without_saving(self) -> None:
        """Quit without saving."""
        Settings.set('save_settings', False)
        self.quit()


class SignalHandler():
    """Handle UNIX signals."""

    def __init__(self, window: Window) -> None:
        """Initialize SignalHandler."""
        self.window = window

    def handle(self, signum: int, frame) -> None:
        """When an UNIX signal gets received, show the window."""
        self.window.show()


class ThemeManager():
    """Manages the theme."""

    def __init__(self, config_retriever: ConfigRetriever) -> None:
        """Initialize the module manager."""
        self.config_retriever = config_retriever
        self.theme_dir = os.path.join(self.config_retriever.get_setting('config_path'), 'themes')
        self.logger = None  # type: Optional[Logger]

    @staticmethod
    def add_prefix(theme_name: str) -> str:
        """Ensure the string starts with pext_theme_."""
        if not theme_name.startswith('pext_theme_'):
            return 'pext_theme_{}'.format(theme_name)

        return theme_name

    @staticmethod
    def remove_prefix(theme_name: str) -> str:
        """Remove pext_theme_ from the start of the string."""
        if theme_name.startswith('pext_theme_'):
            return theme_name[len('pext_theme_'):]

        return theme_name

    def bind_logger(self, logger: Logger) -> None:
        """Connect a logger to the module manager.

        If a logger is connected, the module manager will log all
        messages directly to the logger.
        """
        self.logger = logger

    def _get_palette_mappings(self) -> Dict[str, int]:
        mapping = {'colour_roles': {}, 'colour_groups': {}}  # type: Dict[str, Dict[str, str]]
        for key in dir(QPalette):
            value = getattr(QPalette, key)
            if isinstance(value, QPalette.ColorRole):
                mapping['colour_roles'][key] = value
                mapping['colour_roles'][value] = key
            elif isinstance(value, QPalette.ColorGroup):
                mapping['colour_groups'][key] = value
                mapping['colour_groups'][value] = key

        return mapping

    def list_themes(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Return a list of modules together with their source."""
        return ObjectManager().list_objects(self.theme_dir)

    def load_theme(self, theme_name: str) -> QPalette:
        """Return the parsed palette."""
        theme_name = ThemeManager.add_prefix(theme_name)

        palette = QPalette()
        palette_mappings = self._get_palette_mappings()

        config = configparser.ConfigParser()
        config.optionxform = lambda option: option  # type: ignore  # No lowercase
        config.read(os.path.join(self.theme_dir, theme_name, 'theme.conf'))

        for colour_group in config.sections():
            for colour_role in config[colour_group]:
                colour_code_list = [int(x) for x in config[colour_group][colour_role].split(",")]

                try:
                    palette.setColor(palette_mappings['colour_groups'][colour_group],
                                     palette_mappings['colour_roles'][colour_role],
                                     QColor(*colour_code_list))
                except KeyError as e:
                    print("Theme contained an unknown key, {}, skipping.".format(e))

        return palette

    def apply_theme_to_app(self, palette: QPalette, app: QApplication) -> None:
        """Apply the palette to the app (use this before creating a window)."""
        app.setPalette(palette)

    def install_theme(self, url: str, verbose=False, interactive=True) -> bool:
        """Install a theme."""
        theme_name = url.rstrip("/").split("/")[-1]

        dir_name = ThemeManager.add_prefix(theme_name).replace('.', '_')
        theme_name = ThemeManager.remove_prefix(theme_name)

        if os.path.exists(os.path.join(self.theme_dir, dir_name)):
            if verbose:
                Logger._log('✔⇩ {}'.format(theme_name), self.logger)

            return False

        if verbose:
            Logger._log('⇩ {} ({})'.format(theme_name, url), self.logger)

        try:
            porcelain.clone(UpdateManager.fix_git_url_for_dulwich(url), os.path.join(self.theme_dir, dir_name))
        except Exception as e:
            if verbose:
                Logger._log_error('⇩ {}: {}'.format(theme_name, e), self.logger)

            traceback.print_exc()

            try:
                rmtree(os.path.join(self.theme_dir, dir_name))
            except FileNotFoundError:
                pass

            return False

        if verbose:
            Logger._log('✔⇩ {}'.format(theme_name), self.logger)

        return True

    def uninstall_theme(self, theme_name: str, verbose=False) -> bool:
        """Uninstall a theme."""
        dir_name = ThemeManager.add_prefix(theme_name)
        theme_name = ThemeManager.remove_prefix(theme_name)

        if verbose:
            Logger._log('♻ {}'.format(theme_name), self.logger)

        try:
            rmtree(os.path.join(self.theme_dir, dir_name))
        except FileNotFoundError:
            if verbose:
                Logger._log(
                    '✔♻ {}'.format(theme_name),
                    self.logger)

            return False

        if verbose:
            Logger._log('✔♻ {}'.format(theme_name), self.logger)

        return True

    def update_theme(self, theme_name: str, verbose=False) -> bool:
        """Update a theme."""
        dir_name = ThemeManager.add_prefix(theme_name)
        theme_name = ThemeManager.remove_prefix(theme_name)

        if verbose:
            Logger._log('⇩ {}'.format(theme_name), self.logger)

        try:
            if not UpdateManager.update(os.path.join(self.theme_dir, dir_name)):
                if verbose:
                    Logger._log('⏩{}'.format(theme_name), self.logger)
                return False

        except Exception as e:
            if verbose:
                Logger._log_error(
                    '⇩ {}: {}'.format(theme_name, e),
                    self.logger)

            traceback.print_exc()

            return False

        if verbose:
            Logger._log('✔⇩ {}'.format(theme_name), self.logger)

        return True

    def update_all_themes(self, verbose=False) -> bool:
        """Update all themes."""
        success = True

        for theme in self.list_themes().keys():
            if not self.update_theme(theme, verbose=verbose):
                success = False

        return success


class Tray():
    """Handle the system tray."""

    def __init__(self, window: Window, app_icon: str) -> None:
        """Initialize the system tray."""
        self.window = window

        self.tray = QSystemTrayIcon(app_icon)

        self.tray.activated.connect(self.icon_clicked)

        if Settings.get('profile') == ProfileManager.default_profile_name():
            self.tray.setToolTip('Pext')
        else:
            self.tray.setToolTip('Pext ({})'.format(Settings.get('profile')))

    def icon_clicked(self, reason: int) -> None:
        """Toggle window visibility on left click."""
        if reason == 3:
            self.window.toggle_visibility(force_tray=True)

    def show(self) -> None:
        """Show the tray icon."""
        self.tray.show()

    def hide(self) -> None:
        """Hide the tray icon."""
        self.tray.hide()


class Settings():
    """A globally accessible class that stores all Pext's settings."""

    __settings = {
        '_launch_app': True,  # Keep track if launching is normal
        'background': False,
        'clipboard': 'clipboard',
        'locale': QLocale.system().name(),
        'modules': [],
        'minimize_mode': MinimizeMode.Normal,
        'profile': ProfileManager.default_profile_name(),
        'save_settings': True,
        'sort_mode': SortMode.Module,
        'style': None,
        'theme': None,
        'tray': True,
        'update_check': None  # None = not asked, True/False = permission
    }

    @staticmethod
    def get(name, default=None):
        """Return the value of a single setting, falling back to default if None."""
        value = Settings.__settings[name]
        if value is None:
            return default

        return value

    @staticmethod
    def get_all():
        """Return all settings."""
        return Settings.__settings

    @staticmethod
    def set(name, value):
        """Set a single setting if this setting is known."""
        if name in Settings.__settings:
            Settings.__settings[name] = value
        else:
            raise NameError('{} is not a key of Settings'.format(name))

    @staticmethod
    def update(value):
        """Update the dictionary with new values if any changed."""
        Settings.__settings.update(value)


class ModuleOptionParser(argparse.Action):
    """Parse module options from the command line."""

    def __call__(self, parser, namespace, value, option_string=None):
        """Save the module and appropriate module options in the correct order."""
        if '_modules' not in namespace:
            setattr(namespace, '_modules', [])

        modules = namespace._modules

        if self.dest == 'module':
            modules.append({'name': value, 'settings': {}})
            setattr(namespace, '_modules', modules)
        else:
            modules[-1]['settings'][self.dest[len('module-'):]] = value
            setattr(namespace, '_modules', modules)


def _init_persist(profile: str, background: bool) -> None:
    """Open Pext if an instance is already running.

    Checks if Pext is already running and if so, send it SIGUSR1 to bring it
    to the foreground. If Pext is not already running, saves a PIDfile so that
    another Pext instance can find us.
    """
    lock = ProfileManager.get_lock_instance(profile)
    if lock:
        # Notify the main process if we are not using --background
        if not background:
            if platform.system() == 'Windows':
                print("Pext is already running and foregrounding the running instance is currently not supported "
                      "on Windows. Doing nothing...")
            else:
                os.kill(lock, signal.SIGUSR1)
        else:
            print("Pext is already running, but --background was given. Doing nothing...")

        sys.exit(0)

    ProfileManager.lock_profile(profile)


def _load_settings(argv: List[str], config_retriever: ConfigRetriever) -> None:
    """Load the settings from the command line and set defaults."""
    parser = argparse.ArgumentParser(description='The Python-based extendable tool.')
    parser.add_argument('-v', '--version', action='version',
                        version='Pext {}'.format(UpdateManager().get_core_version()))
    parser.add_argument('--locale', help='load the given locale.')
    parser.add_argument('--list-styles', action='store_true',
                        help='print a list of loadable Qt system styles and exit.')
    parser.add_argument('--style', help='use the given Qt system style for the UI.')
    parser.add_argument('--background', action='store_true', help='do not open the user interface this invocation.')
    parser.add_argument('--clipboard', '-c', choices=['clipboard', 'selection'],
                        help='choose the clipboard to copy entries to.')
    parser.add_argument('--module', '-m', action=ModuleOptionParser,
                        help='name the module to use. This option may be given multiple times. '
                        'When this option is given, the profile module list will be overwritten.')
    parser.add_argument('--install-module', action='append',
                        help='download and install a module from the given git URL.')
    parser.add_argument('--uninstall-module', action='append', help='uninstall the given module.')
    parser.add_argument('--update-module', action='append', help='update the given module.')
    parser.add_argument('--update-modules', action='store_true', help='update all modules.')
    parser.add_argument('--list-modules', action='store_true', help='list all installed modules.')
    parser.add_argument('--theme', help='use the chosen theme.')
    parser.add_argument('--install-theme', action='append',
                        help='download and install a theme from the given git URL.')
    parser.add_argument('--uninstall-theme', action='append', help='uninstall a theme by name.')
    parser.add_argument('--update-theme', action='append', help='update the chosen theme.')
    parser.add_argument('--update-themes', action='store_true', help='update all themes.')
    parser.add_argument('--list-themes', action='store_true', help='list all installed themes.')
    parser.add_argument('--profile', '-p',
                        help='use the chosen profile, creating it if it doesn\'t exist yet. '
                        'Defaults to "default", use "none" to not save the application state between runs.')
    parser.add_argument('--create-profile', action='append', help='create a new profile with the given name.')
    parser.add_argument('--remove-profile', action='append', help='remove the chosen profile.')
    parser.add_argument('--rename-profile', nargs=2, action='append', help='rename the chosen profile.')
    parser.add_argument('--list-profiles', action='store_true', help='list all profiles.')
    parser.add_argument('--tray', action='store_true', dest='tray', help='create a tray icon (this is the default).')
    parser.add_argument('--no-tray', action='store_false', dest='tray', help='do not create a tray icon.')

    # Ensure module options get parsed
    for arg in argv:
        arg = arg.split("=")[0]
        if arg.startswith("--module-"):
            try:
                parser.add_argument(arg, action=ModuleOptionParser)
            except argparse.ArgumentError:
                # Probably already added
                pass

    args = parser.parse_args()

    # First, check for profile
    if args.profile:
        Settings.set('profile', args.profile)

    # Create directory for profile if not existant
    try:
        ProfileManager(config_retriever).create_profile(str(Settings.get('profile')))
    except OSError:
        pass

    # Load all from profile
    Settings.update(ProfileManager(config_retriever).retrieve_settings(str(Settings.get('profile'))))

    # Then, check for the rest
    if args.locale:
        Settings.set('locale', args.locale)

    if args.list_styles:
        for style in QStyleFactory().keys():
            print(style)

        Settings.set('_launch_app', False)

    if args.style:
        if args.style in QStyleFactory().keys():
            Settings.set('style', args.style)
        else:
            # PyQt5 does not have bindings for QQuickStyle yet
            os.environ["QT_QUICK_CONTROLS_STYLE"] = args.style

    if args.background:
        Settings.set('background', True)

    if args.clipboard:
        Settings.set('clipboard', args.clipboard)

    if args.install_module:
        for module in args.install_module:
            if not ModuleManager(config_retriever).install_module(module, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.uninstall_module:
        for module in args.uninstall_module:
            if not ModuleManager(config_retriever).uninstall_module(module, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.update_module:
        for module in args.update_module:
            if not ModuleManager(config_retriever).update_module(module, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.update_modules:
        if not ModuleManager(config_retriever).update_all_modules(verbose=True):
            sys.exit(3)

        Settings.set('_launch_app', False)

    if args.list_modules:
        for module_name, module_data in ModuleManager(config_retriever).list_modules().items():
            print('{} ({})'.format(module_name, module_data['source']))

        Settings.set('_launch_app', False)

    if args.theme:
        Settings.set('theme', args.themes)

    if args.install_theme:
        for theme in args.install_theme:
            if not ThemeManager(config_retriever).install_theme(theme, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.uninstall_theme:
        for theme in args.uninstall_theme:
            if not ThemeManager(config_retriever).uninstall_theme(theme, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.update_theme:
        for theme in args.update_theme:
            if not ThemeManager(config_retriever).update_theme(theme, verbose=True):
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.update_themes:
        if not ThemeManager(config_retriever).update_all_themes(verbose=True):
            sys.exit(3)

        Settings.set('_launch_app', False)

    if args.list_themes:
        for theme_name, theme_data in ThemeManager(config_retriever).list_themes().items():
            print('{} ({})'.format(theme_name, theme_data['source']))

        Settings.set('_launch_app', False)

    if args.create_profile:
        for profile in args.create_profile:
            if not ProfileManager(config_retriever).create_profile(profile):
                print('Could not create profile {}, it already exists.'.format(profile))
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.remove_profile:
        for profile in args.remove_profile:
            if not ProfileManager(config_retriever).remove_profile(profile):
                print('Could not delete profile {}, it is in use.'.format(profile))
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.rename_profile:
        for old_name, new_name in args.rename_profile:
            if not ProfileManager(config_retriever).rename_profile(old_name, new_name):
                print('Could not rename profile {} to {}.'.format(old_name, new_name))
                sys.exit(3)

        Settings.set('_launch_app', False)

    if args.list_profiles:
        for profile in ProfileManager(config_retriever).list_profiles():
            print(profile)

        Settings.set('_launch_app', False)

    if args.tray is not None:
        Settings.set('tray', args.tray)

    # Set up the parsed modules
    if '_modules' in args:
        Settings.set('modules', args._modules)

    # See if automatic update checks are allowed
    if config_retriever.get_updatecheck_permission_asked():
        Settings.set('update_check', config_retriever.get_updatecheck_permission())


def _shut_down(window: Window, config_retriever: ConfigRetriever) -> None:
    """Clean up."""
    for module in window.tab_bindings:
        try:
            module['module'].stop()
        except Exception as e:
            print("Failed to cleanly stop module {}: {}".format(module['module_name'], e))
            traceback.print_exc()

    profile = Settings.get('profile')
    ProfileManager.unlock_profile(profile)

    if Settings.get('save_settings'):
        ProfileManager(config_retriever).save_modules(profile, window.tab_bindings)
        ProfileManager(config_retriever).save_settings(profile)


def main() -> None:
    """Start the application."""
    # Load configuration
    config_retriever = ConfigRetriever()

    # Ensure our necessary directories exist
    for directory in ['modules',
                      'module_dependencies',
                      'themes',
                      'profiles',
                      os.path.join('profiles', ProfileManager.default_profile_name())]:
        try:
            os.makedirs(os.path.join(config_retriever.get_setting('config_path'), directory))
        except OSError:
            # Probably already exists, that's okay
            pass

    # Delete old system theme hack if exists
    # TODO: Remove later
    try:
        rmtree(os.path.join(config_retriever.get_setting('config_path'), 'themes', "pext_theme_system"))
    except FileNotFoundError:
        # Probably already deleted
        pass

    _load_settings(sys.argv[1:], config_retriever)

    if not Settings.get('_launch_app'):
        sys.exit(0)

    # Warn if we may get UI issues
    if warn_no_openGL_linux:
        print("python3-opengl is not installed. If Pext fails to render, please try installing it. "
              "See https://github.com/Pext/Pext/issues/11.")

    # Set up persistence
    _init_persist(Settings.get('profile'), Settings.get('background'))

    # Set up the app
    if Settings.get('profile') == ProfileManager.default_profile_name():
        appname = 'Pext'
    else:
        appname = 'Pext ({})'.format(Settings.get('profile'))
    app = QApplication([appname])

    translator = QTranslator()
    locale_to_use = Settings.get('locale')
    print('Using locale: {} {}'
          .format(QLocale(locale_to_use).name(),
                  "(manually set)" if Settings.get('locale') != QLocale.system().name() else ""))
    print('Localization loaded:',
          translator.load(QLocale(locale_to_use), 'pext', '_', os.path.join(AppFile.get_path(), 'i18n'), '.qm'))

    app.installTranslator(translator)

    # Load the app icon
    # KDE doesn't support svg in the system tray and macOS makes the png in
    # the dock yellow. Let's assume svg for macOS and PNG for Linux for now.
    if platform.system() == "Darwin":
        app_icon = QIcon(os.path.join(AppFile.get_path(), 'images', 'scalable', 'pext.svg'))
    else:
        app_icon = QIcon(os.path.join(AppFile.get_path(), 'images', '128x128', 'pext.png'))

    app.setWindowIcon(app_icon)

    if Settings.get('style') is not None:
        app.setStyle(QStyleFactory().create(Settings.get('style')))

    theme_name = Settings.get('theme')
    if theme_name is not None:
        theme_manager = ThemeManager(config_retriever)
        theme = theme_manager.load_theme(theme_name)
        theme_manager.apply_theme_to_app(theme, app)

    # Check if clipboard is supported
    if Settings.get('clipboard') == 'selection' and not app.clipboard().supportsSelection():
        print("Requested clipboard type is not supported")
        sys.exit(3)

    # Get a window
    window = Window(config_retriever)

    # Get a logger
    logger = Logger(window)

    # Give the window a reference to the logger
    window.bind_logger(logger)

    # Clean up on exit
    atexit.register(_shut_down, window, config_retriever)

    # Handle SIGUSR1 UNIX signal
    signal_handler = SignalHandler(window)
    if not platform.system() == 'Windows':
        signal.signal(signal.SIGUSR1, signal_handler.handle)

    # Create a main loop
    main_loop = MainLoop(app, window, logger)

    # Create a tray icon
    # This needs to be stored in a variable to prevent the Python garbage collector from removing the Qt tray
    tray = Tray(window, app_icon)  # noqa: F841

    # Give the window a reference to the tray
    window.bind_tray(tray)

    # And run...
    main_loop.run()


if __name__ == "__main__":
    main()
