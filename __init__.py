# Copyright 2018 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import astral
import time
import arrow
import json
from pytz import timezone
from datetime import datetime

from mycroft.messagebus.message import Message
from mycroft.skills.core import MycroftSkill
from mycroft.util import get_ipc_directory
from mycroft.util.log import LOG
from mycroft.util.parse import normalize
from mycroft import intent_file_handler

import os
import subprocess

import pyaudio
from threading import Thread, Lock

class RemotePlatform(MycroftSkill):
    """
        The RemotePlatform skill handles much of the gui activities related to
        Mycroft's core functionality. This includes showing "listening",
        "thinking", and "speaking" faces as well as more complicated things
        such as switching to the selected resting face and handling
        system signals.
    """
    def __init__(self):
        super().__init__('RemotePlatform')

        self.idle_screens = {}
        self.override_idle = None
        self.idle_next = 0  # Next time the idle screen should trigger
        self.idle_lock = Lock()

        self.settings['auto_brightness'] = False
        self.settings['use_listening_beep'] = True

        self.has_show_page = False  # resets with each handler

        self.skill_setting_obj = {}
        self.skill_setting_list = []

    def initialize(self):
        """ Perform initalization.

            Registers messagebus handlers and sets default gui values.
        """
        # Prepare GUI Viseme structure
        self.gui['viseme'] = {'start': 0, 'visemes': []}

        # Preselect Time and Date as resting screen
        self.gui['selected'] = self.settings.get('selected', 'AndroidHomescreen')
        self.gui.set_on_gui_changed(self.save_resting_screen)
        self.gui.register_handler('mycroft.gui.screen.close', self.force_idle_screen)
        self.add_event('mycroft.gui.screen.close', self.force_idle_screen)
        self.bus.on('mycroft.gui.screen.close', self.force_idle_screen)
        self.add_event('mycroft.gui.forceHome', self.force_home)

        try:
            #self.gui.register_handler('mycroft.gui.screen.close', self.show_home_screen)
            # Handle the 'waking' visual
            self.add_event('recognizer_loop:record_end',
                           self.handle_listener_ended)
            self.add_event('mycroft.speech.recognition.unknown',
                           self.handle_failed_stt)

            # Handle the 'busy' visual
            self.bus.on('mycroft.skill.handler.start',
                        self.on_handler_started)

            self.bus.on('recognizer_loop:sleep',
                        self.on_handler_sleep)
            self.bus.on('mycroft.awoken',
                        self.on_handler_awoken)
            self.bus.on('enclosure.mouth.reset',
                        self.on_handler_mouth_reset)
            self.bus.on('recognizer_loop:audio_output_end',
                        self.on_handler_mouth_reset)
            self.bus.on('enclosure.mouth.viseme_list',
                        self.on_handler_speaking)
            self.bus.on('gui.page.show',
                        self.on_gui_page_show)
            self.bus.on('gui.page_interaction', self.on_gui_page_interaction)

            self.bus.on('mycroft.skills.initialized', self.reset_face)
            self.bus.on('mycroft.mark2.register_idle',
                        self.on_register_idle)

            self.bus.on('gui.skill.settings.show',
                        self.handle_skill_setting_show)

            # Handle device settings events
            self.add_event('mycroft.device.settings',
                           self.handle_device_settings)

            # Use Legacy for QuickSetting delegate
            self.gui.register_handler('mycroft.device.settings',
                                      self.handle_device_settings)
            self.gui.register_handler('mycroft.device.settings.homescreen',
                                      self.handle_device_homescreen_settings)
            self.gui.register_handler('mycroft.device.show.idle',
                                      self.show_idle_screen)
            self.gui.register_handler('mycroft.device.settings.skillconfig',
                                      self.handle_device_skill_settings)

            # Handle idle selection
            self.gui.register_handler('mycroft.device.set.idle',
                                      self.set_idle_screen)

            # Show loading screen while starting up skills.
            self.gui['state'] = 'loading'
            self.gui.show_page('all.qml')

            # Collect Idle screens and display if skill is restarted
            self.collect_resting_screens()
        except Exception:
            LOG.exception('In Remote Platform Skill')


    ###################################################################
    # Idle screen mechanism
    def force_home(self, message):
        screen = self.idle_screens.get(self.gui['selected'])
        if screen:
            self.bus.emit(Message('{}.idle'.format(screen)))

    @intent_file_handler('homescreen.intent')
    def call_home_from_voc(self):
        self.log.debug("back button pressed")
        self.force_idle_screen()

    def show_home_screen(self):
        self.log.debug("back button pressed")
        self.gui.clear()
        self.enclosure.display_manager.remove_active()
        self.force_idle_screen()

    def save_resting_screen(self):
        """ Handler to be called if the settings are changed by
            the GUI.

            Stores the selected idle screen.
        """
        self.log.debug("Saving resting screen")
        self.settings['selected'] = self.gui['selected']
        self.gui['selectedScreen'] = self.gui['selected']

    def collect_resting_screens(self):
        """ Trigger collection and then show the resting screen. """
        self.bus.emit(Message('mycroft.mark2.collect_idle'))
        time.sleep(1)
        self.show_idle_screen()

    def on_register_idle(self, message):
        """ Handler for catching incoming idle screens. """
        if 'name' in message.data and 'id' in message.data:
            self.idle_screens[message.data['name']] = message.data['id']
            self.log.info('Registered {}'.format(message.data['name']))
        else:
            self.log.error('Malformed idle screen registration received')

    def reset_face(self, message):
        """ Triggered after skills are initialized.

            Sets switches from resting "face" to a registered resting screen.
        """
        time.sleep(1)
        self.collect_resting_screens()

    def stop(self, message=None):
        """ Clear override_idle and stop visemes. """
        if (self.override_idle and
                time.monotonic() - self.override_idle[1] > 2):
            self.override_idle = None
            self.show_idle_screen()
        self.gui['viseme'] = {'start': 0, 'visemes': []}
        return False

    def shutdown(self):
        # Gotta clean up manually since not using add_event()
        self.bus.remove('mycroft.skill.handler.start',
                        self.on_handler_started)
        self.bus.remove('recognizer_loop:sleep',
                        self.on_handler_sleep)
        self.bus.remove('mycroft.awoken',
                        self.on_handler_awoken)
        self.bus.remove('enclosure.mouth.reset',
                        self.on_handler_mouth_reset)
        self.bus.remove('recognizer_loop:audio_output_end',
                        self.on_handler_mouth_reset)
        self.bus.remove('enclosure.mouth.viseme_list',
                        self.on_handler_speaking)
        self.bus.remove('gui.page.show',
                        self.on_gui_page_show)
        self.bus.remove('gui.page_interaction', self.on_gui_page_interaction)
        self.bus.remove('mycroft.mark2.register_idle', self.on_register_idle)

    #####################################################################
    # Manage "busy" visual

    def on_handler_started(self, message):
        handler = message.data.get("handler", "")
        # Ignoring handlers from this skill and from the background clock
        if 'RemotePlatform' in handler:
            return
        if 'TimeSkill.update_display' in handler:
            return

    def on_gui_page_interaction(self, message):
        """ Reset idle timer to 30 seconds when page is flipped. """
        self.log.info("Resetting idle counter to 30 seconds")
        self.start_idle_event(30)

    def on_gui_page_show(self, message):
        if 'remote-platform' not in message.data.get('__from', ''):
            # Some skill other than the handler is showing a page
            self.has_show_page = True

            # If a skill overrides the idle do not switch page
            override_idle = message.data.get('__idle')
            if override_idle is True:
                # Disable idle screen
                #self.log.info('Cancelling Idle screen')
                self.cancel_idle_event()
                self.override_idle = (message, time.monotonic())
            elif isinstance(override_idle, int):
                # Set the indicated idle timeout
                self.log.info('Overriding idle timer to'
                              ' {} seconds'.format(override_idle))
                self.start_idle_event(override_idle)
            elif (message.data['page'] and
                    not message.data['page'][0].endswith('idle.qml')):
                # Set default idle screen timer
                self.start_idle_event(30)

    def on_handler_mouth_reset(self, message):
        """ Restore viseme to a smile. """
        pass

    def on_handler_sleep(self, message):
        """ Show resting face when going to sleep. """
        self.gui['state'] = 'resting'
        self.gui.show_page('all.qml')

    def on_handler_awoken(self, message):
        """ Show awake face when sleep ends. """
        self.gui['state'] = 'awake'
        self.gui.show_page('all.qml')

    def on_handler_complete(self, message):
        """ When a skill finishes executing clear the showing page state. """
        handler = message.data.get('handler', '')
        # Ignoring handlers from this skill and from the background clock
        if 'RemotePlatform' in handler:
            return
        if 'TimeSkill.update_display' in handler:
            return

        self.has_show_page = False

        try:
            if self.hourglass_info[handler] == -1:
                self.enclosure.reset()
            del self.hourglass_info[handler]
        except Exception:
            # There is a slim chance the self.hourglass_info might not
            # be populated if this skill reloads at just the right time
            # so that it misses the mycroft.skill.handler.start but
            # catches the mycroft.skill.handler.complete
            pass

    #####################################################################
    # Manage "speaking" visual

    def on_handler_speaking(self, message):
        """ Show the speaking page if no skill has registered a page
            to be shown in it's place.
        """
        self.gui["viseme"] = message.data
        if not self.has_show_page:
            self.gui['state'] = 'speaking'
            self.gui.show_page("all.qml")
            # Show idle screen after the visemes are done (+ 2 sec).
            time = message.data['visemes'][-1][1] + 5
            self.start_idle_event(time)

    #####################################################################
    # Manage "idle" visual state
    def cancel_idle_event(self):
        self.idle_next = 0
        self.cancel_scheduled_event('IdleCheck')

    def start_idle_event(self, offset=60, weak=False):
        """ Start an event for showing the idle screen.

        Arguments:
            offset: How long until the idle screen should be shown
            weak: set to true if the time should be able to be overridden
        """
        with self.idle_lock:
            if time.monotonic() + offset < self.idle_next:
                self.log.info('No update, before next time')
                return

            self.log.info('Starting idle event')
            try:
                if not weak:
                    self.idle_next = time.monotonic() + offset
                # Clear any existing checker
                self.cancel_scheduled_event('IdleCheck')
                time.sleep(0.5)
                self.schedule_event(self.show_idle_screen, int(offset),
                                    name='IdleCheck')
                self.log.info('Showing idle screen in '
                              '{} seconds'.format(offset))
            except Exception as e:
                self.log.exception(repr(e))

    def show_idle_screen(self):
        """ Show the idle screen or return to the skill that's overriding idle.
        """
        self.log.debug('Showing idle screen')
        screen = None
        if self.override_idle:
            self.log.debug('Returning to override idle screen')
            # Restore the page overriding idle instead of the normal idle
            self.bus.emit(self.override_idle[0])
        elif len(self.idle_screens) > 0 and 'selected' in self.gui:
            # TODO remove hard coded value
            self.log.debug('Showing Idle screen for '
                           '{}'.format(self.gui['selected']))
            screen = self.idle_screens.get(self.gui['selected'])
        if screen:
            self.bus.emit(Message('{}.idle'.format(screen)))

    def force_idle_screen(self, _=None):
        if (self.override_idle and time.monotonic() - self.override_idle[1] > 2):
            self.override_idle = None
            self.show_idle_screen()
        else:
            self.show_idle_screen()

    def handle_listener_ended(self, message):
        """ When listening has ended show the thinking animation. """
        self.has_show_page = False
        self.gui['state'] = 'thinking'
        self.gui.show_page('all.qml')

    def handle_failed_stt(self, message):
        """ No discernable words were transcribed. Show idle screen again. """
        self.show_idle_screen()

    #####################################################################
    # Device Settings

    @intent_file_handler('device.settings.intent')
    def handle_device_settings(self, message):
        """ Display device settings page. """
        self.gui['state'] = 'settings/settingspage'
        self.gui['skillConfig'] = self.skill_setting_obj
        self.gui.show_page('all.qml')

    @intent_file_handler('device.homescreen.settings.intent')
    def handle_device_homescreen_settings(self, message):
        """
            display homescreen settings page
        """
        screens = [{'screenName': s, 'screenID': self.idle_screens[s]}
                   for s in self.idle_screens]
        self.gui['idleScreenList'] = {'screenBlob': screens}
        self.gui['selectedScreen'] = self.gui['selected']
        self.gui['state'] = 'settings/homescreen_settings'
        self.gui.show_page('all.qml')

    def set_idle_screen(self, message):
        """ Set selected idle screen from message. """
        self.gui['selected'] = message.data['selected']
        self.save_resting_screen()

    def handle_device_update_settings(self, message):
        """ Display device update settings page. """
        self.gui['state'] = 'settings/updatedevice_settings'
        self.gui.show_page('all.qml')

    def handle_skill_setting_show(self, message):
        """ Handle build skill settings display """
        if (message.data["method"] == "set"):

            self.skill_setting_list.append({"skill_id":
                                            message.data["skill_id"],
                                            "setting_key":
                                                message.data["setting_key"],
                                            "setting_type":
                                                message.data["setting_type"],
                                            "current_value":
                                                message.data["current_value"],
                                            "available_values":
                                                message.data
                                                ["available_values"]})

        elif (message.data["method"] == "update"):
            a = next(item for item in self.skill_setting_list
                     if item["skill_id"] == message.data["skill_id"])
            a["current_value"] = message.data["current_value"]
            self.log.info(a)

        else:
            self.log.error("no method defined")

        self.skill_setting_obj["configs"] = self.skill_setting_list
        self.gui['skillConfig'] = json.dumps(skill_setting_obj)

    def handle_device_skill_settings(self, message):
        self.gui['skillConfig'] = self.skill_setting_obj
        self.gui['state'] = 'settings/skill_settings'
        self.gui.show_page('all.qml')


def create_skill():
    return RemotePlatform()
