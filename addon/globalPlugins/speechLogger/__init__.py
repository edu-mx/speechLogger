# NVDA Speech Logger add-on, V22.0
#
#    Copyright (C) 2022 Luke Davis <XLTechie@newanswertech.com>, James Scholes
#
# This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License
# as published by    the Free Software Foundation; either version 2 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""An NVDA add-on which logs, in real time, spoken output to a file or files.
This can include any output generated using the NVDA remote add-on.
Lightly based on suggestions sent to the nvda-addons@groups.io mailing list by James Scholes (https://nvda-addons.groups.io/g/nvda-addons/message/18552).

This add-on must be configured before use. Configure it in NVDA Preferences -> Settings -> Speech Logger.

You have to define your own toggle gestures for this add-on, under the NVDA Input Gestures Tools category.
Look for "Toggles logging of local speech" and "Toggles logging of remote speech".

The log files are opened and closed for each speech utterance, because the original mandate for this add-on
was to have real-time saving of output.
Be warned that means lots of disk activity.
"""

import os
from functools import wraps
from enum import Enum, unique, auto, IntEnum

import addonHandler
import globalPluginHandler
import globalPlugins
import ui
import gui
import config
import speech
from speech.types import SpeechSequence, Optional
from speech.priorities import Spri
from scriptHandler import script
from logHandler import log

from .configUI import SpeechLoggerSettings
from .immutableKeyObj import ImmutableKeyObj

addonHandler.initTranslation()
	

@unique
class Origin(Enum):
	"""Enum to tell our methods where a speech sequence came from."""
	LOCAL = auto()
	REMOTE = auto()


def deblog(message: str):
	"""Crude debug log appender. Disable by uncommenting the return statement."""
	return  # Don't log anything; production code should use this.
	file = os.path.abspath(os.path.expandvars(r"%temp%\lukeslog.txt"))
	with open(file, "a+", encoding="utf-8") as f:
		f.write(message + "\n")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self):
		super().__init__()
		deblog("Initializing.")
		# Runtime vars and their sane defaults:
		# Because our runtime flags and variables are many and confusing,
		# We try to prevent some errors by using Immutable Key Objects to hold them where posible.
		self.flags = ImmutableKeyObj(
			# Do we intend to log local speech?
			logLocal=False,
			# Tracks whether we are actively logging local speech
			localActive=False,
			# Do we intend to log remote speech?
			logRemote=False,
			# Tracks whether we are actively logging remote speech
			remoteActive=False,
			# Has the NVDA Remote speech capturing callback been registered?
			callbackRegistered=False,
			# Should we rotate logs on startup?
			rotate=False
		)
		#: Filenames are obtained from NVDA configuration, and setup in applyUserConfig().
		self.files = ImmutableKeyObj(local=None, remote=None)
		# We can't handle getting our callback into NVDA Remote during __init__,
		# because remoteClient doesn't show up in globalPlugins yet. We will do it in the script instead.
		#: Holds an initially empty reference to NVDA Remote
		self.remotePlugin = None
		# Establish the add-on's NVDA configuration panel and config options
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(SpeechLoggerSettings)
		# Read user config or defaults
		self.applyUserConfig()
		# If we are supposed to rotate logs, do that now.
		if self.flags.rotate:
			self.rotateLogs()
		# Wrap speech.speech.speak, so we can get its output first
		old_speak = speech.speech.speak
		@wraps(speech.speech.speak)
		def new_speak(  # noqa: C901
				sequence: SpeechSequence,
				symbolLevel: Optional[int] = None,
				priority: Spri = Spri.NORMAL
		):
			#deblog("In wrapped speak.")
			self.captureSpeech(sequence, Origin.LOCAL)
			return old_speak(sequence, symbolLevel, priority)
		speech.speech.speak = new_speak

	def terminate(self):
		# Remove the NVDA settings panel
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(SpeechLoggerSettings)

	def applyUserConfigIfNeeded(self):
		"""If the user has changed any part of the configuration, reset our internals accordingly."""
		if SpeechLoggerSettings.hasConfigChanges:
			self.applyUserConfig()
			SpeechLoggerSettings.hasConfigChanges = False

	def applyUserConfig(self):
		"""Configures internal variables according to those set in NVDA config."""
		# Stage 1: directory
		# We shouldn't be able to reach this point with a bad directory name, unless
		# the user has been hand-editing nvda.ini. However, since that's possible, we must check.
		if not os.path.exists(os.path.abspath(os.path.expandvars(config.conf['speechLogger']['folder']))):
			# Notify the user
			log.error(f"The folder given for log files does not exist ({config.conf['speechLogger']['folder']}).")
			# Disable all logging
			self.flags.logLocal = False
			self.flags.logRemote = False
			# Nothing else matters.
			return
		# Stage 2: files
		# If either filename is empty, it means the user doesn't want logging for that type.
		if config.conf['speechLogger']['local'] == "":
			self.flags.logLocal = False
			self.files.local = None
		else:
			self.flags.logLocal = True
			self.files.local = os.path.join(
				os.path.abspath(os.path.expandvars(config.conf['speechLogger']['folder'])),
				os.path.basename(os.path.expandvars(config.conf['speechLogger']['local']))
			)
			# Test open
			try:
				with open(self.files.local, "a+", encoding="utf-8") as f:
					pass
			except Exception as e:
				log.error(f"Couldn't open local log file {self.files.local} for appending. {e}")
				self.files.local = None
				self.flags.logLocal = False
		if config.conf['speechLogger']['remote'] == "":
			self.flags.logRemote = False
			self.files.remote = None
		else:
			self.flags.logRemote = True
			self.files.remote = os.path.join(
				os.path.abspath(os.path.expandvars(config.conf['speechLogger']['folder'])),
				os.path.basename(os.path.expandvars(config.conf['speechLogger']['remote']))
			)
			# Test open
			try:
				with open(self.files.remote, "a+", encoding="utf-8") as test:
					pass
			except Exception as e:
				log.error(f"Couldn't open remote log file {self.files.remote} for appending. {e}")
				self.files.remote = None
				self.flags.logRemote = False
		# Stage 3: file rotation
		# This is handled by __init__() and rotateLogs(); we just update the flag.
		self.flags.rotate = config.conf['speechLogger']['rotate']
		# Stage 4: utterance separation
		# For this one we may need the configured custom separator. However, it seems that
		# some part of NVDA or Configobj, escapes escape chars such as \t. We must undo that.
		unescapedCustomSeparator = config.conf['speechLogger']['customSeparator'].encode().decode("unicode_escape")
		separators = {
			"2spc": "  ",
			"nl": "\n",
			"comma": ", ",
			"__": "__",
			"custom": unescapedCustomSeparator
		}
		log.debug(f"###\n{', '.join(str(ord(c)) for c in config.conf['speechLogger']['customSeparator'])}\n{', '.join(str(ord(c)) for c in unescapedCustomSeparator)}")
		# In case the user has gone munging the config file, we must catch key errors.
		try:
			self.utteranceSeparator = separators[config.conf['speechLogger']['separator']]
		except KeyError:
			log.error(
				f'Value "{config.conf["speechLogger"]["separator"]}", found in NVDA config, is '
				'not a known separator. Using default of two spaces.'
			)
			self.utteranceSeparator = separators["2spc"]

	def captureSpeech(self, sequence: SpeechSequence, origin: Origin):
		"""Receives incoming local or remote speech, and if we are capturing that kind, sends it to the appropriate file."""
		self.applyUserConfigIfNeeded()
		file = None
		if origin == Origin.LOCAL and self.flags.localActive:
			file = self.files.local
		elif origin == Origin.REMOTE and self.flags.remoteActive:
			file = self.files.remote
		if file is not None:
			self.logToFile(sequence, file)
		#deblog(f"In captureSpeech. Type is: {origin}, and file is: {file},\nFlags: {self.flags}.")

	def _captureRemoteSpeech(self, *args, **kwargs):
		"""Register this as a callback to the NVDA Remote add-on's speech system, to obtain what it speaks."""
		deblog("In _captureRemoteSpeech.")
		if 'sequence' in kwargs:
			self.captureSpeech(kwargs.get('sequence'), Origin.REMOTE)

	def _obtainRemote(self) -> bool:
		"""Gets us a reference to the NVDA Remote add-on, if available.
		Returns True if we got (or had) one, False otherwise.
		"""
		deblog("In _obtainRemote.")
		# If we already have it, we don't need to get it
		if self.remotePlugin is not None:
			deblog("_obtainRemote: already have it, returning true.")
			return True
		# Find out if NVDA Remote is running, and get a reference if so:
		try:
			for plugin in globalPluginHandler.runningPlugins:
				if isinstance(plugin, globalPlugins.remoteClient.GlobalPlugin):
					self.remotePlugin = plugin
					deblog("_obtainRemote: found it, returning True.")
					return True  # break
		except AttributeError:  # NVDA Remote is not running
			deblog("_obtainRemote: couldn't find it, returning False.")
			return False

	def _registerCallback(self) -> bool:
		"""Adds our callback to NVDA Remote, if possible."""
		deblog("In _registerCallback.")
		# If we have a reference to the Remote plugin, register a handler for its speech:
		if self.remotePlugin is not None:
			# If we already registered a callback, we're done early.
			# FixMe: should deregister the callback on session shutdown.
			if self.flags.callbackRegistered:
					return True
			deblog("_registerCallback: attempting to assign the callback.")
			try:
				self.remotePlugin.master_session.transport.callback_manager.register_callback('msg_speak', self._captureRemoteSpeech)
				self.flags.callbackRegistered = True
				startedRemoteLogging = True
				deblog("_registerCallback: success.")
			except:  # Couldn't do, probably disconnected
				startedRemoteLogging = False
				deblog("_registerCallback: failed.")
		else:
			startedRemoteLogging = False
			deblog("_registerCallback: didn't have a Remote plugin reference, couldn't try.")
		return startedRemoteLogging

	@script(
		category="Tools",
		# Translators: the description of an item in the input gestures tools category
		description=_("Toggles logging of local speech")
	)
	def script_toggleLocalSpeechLogging(self, gesture):
		"""Toggles whether we are actively logging local speech."""
		deblog(f"In local toggle script. Capture was {self.flags.localActive}.")
		if self.flags.localActive:  # Currently logging, stop
			deblog("Local toggle script: stopping.")
			self.flags.localActive = False
			# Translators: message to tell the user that we are no longer logging.
			ui.message(_("Stopped logging local speech."))
		else:  # Currently not logging, start
			deblog(f"Local toggle script: starting, Flags: {self.flags}.")
			# Must check whether we can or should log
			if self.flags.logLocal:
				self.flags.localActive = True
				# Translators: a message to tell the user that we are now logging.
				ui.message(_("Started logging local speech."))
			else:
				deblog(f"Local toggle script: failed to start, Flags: {self.flags}.")
				# Translators: a message to tell the user that we can't start this kind of logging
				ui.message(_("Unable to log local speech. Check NVDA log for more information."))

	@script(
		category="Tools",
		# Translators: the description of an item in the input gestures tools category
		description=_("Toggles logging of remote speech")
	)
	def script_toggleRemoteSpeechLogging(self, gesture):
		"""Toggles whether we are actively logging remote speech."""
		deblog(f"In remote toggle script. Flags: {self.flags}.")
		if self.flags.remoteActive:  # We were logging, stop
			self.flags.remoteActive = False
			# Translators: message to tell the user that we are no longer logging.
			ui.message(_("Stopped logging remote speech."))
		else:  # We weren't logging, start
			deblog(f"Remote toggle script: starting, flags: {self.flags}.")
			# Must check whether we can or should log
			if self.flags.logRemote:
				# If this is the first time we're trying to start capturing,
				# we need to initialize our NVDA Remote interface.
				deblog("Remote toggle script: attempting to start, checking for remote.")
				if self._obtainRemote():
					# We have obtained (or already had) a reference to NVDA Remote. Configure the callback.
					deblog("Remote toggle script: setting up the callback.")
					if self._registerCallback():
						self.flags.remoteActive = True
						deblog("Remote toggle script: success.")
						# Translators: a message to tell the user that we are now logging.
						ui.message(_("Started logging remote speech."))
					else:
						deblog("Remote toggle script:  failed to register the callback.")
						# Translators: a message to tell the user that we can not start logging because remote may not be connected..
						ui.message(_("Could not log remote speech, probably not connected."))
				else:  # self._obtainRemote() returned False
					deblog("Remote toggle script: _obtainRemote() failed.")
					# Translators: a message telling the user that the Remote add-on is unavailable.
					ui.message(_("Failed! Could not find the NVDA Remote add-on."))
			else:
				deblog(f"Remote toggle script: can't do that kind of logging. Flags: {self.flags}\nFiles: {self.files}.")
				# Translators: a message to tell the user that we can't start this kind of logging
				ui.message(_("Unable to log local speech. Check NVDA log for more information."))

	def logToFile(self, sequence: SpeechSequence, file: str):
		"""Append text of the given speech sequence to the given file."""
		deblog(f"In logToFile, logging to {file}")
		with open(file, "a+", encoding="utf-8") as f:
			f.write(self.utteranceSeparator.join(
				toSpeak for toSpeak in sequence if isinstance(toSpeak, str)
			) + "\n")

	def rotateLogs(self):
		"""Not implemented."""
		pass
