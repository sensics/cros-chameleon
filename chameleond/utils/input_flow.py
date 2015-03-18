# Copyright (c) 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Input flow module which abstracts the entire flow for a specific input."""

import itertools
import logging
import time
from abc import ABCMeta

import chameleon_common  # pylint: disable=W0611
from chameleond.utils import audio_utils
from chameleond.utils import common
from chameleond.utils import edid
from chameleond.utils import fpga
from chameleond.utils import frame_manager
from chameleond.utils import ids
from chameleond.utils import io
from chameleond.utils import rx


class InputFlowError(Exception):
  """Exception raised when any error on InputFlow."""
  pass


class InputFlow(object):
  """An abstraction of the entire flow for a specific input.

  It provides the basic interfaces of Chameleond driver for a specific input.
  Using this abstraction, each flow can have its own behavior. No need to
  share the same Chameleond driver code.
  """
  __metaclass__ = ABCMeta

  _CONNECTOR_TYPE = 'Unknown'  # A subclass should override it.

  _RX_SLAVES = {
      ids.DP1: rx.DpRx.SLAVE_ADDRESSES[0],
      ids.DP2: rx.DpRx.SLAVE_ADDRESSES[1],
      ids.HDMI: rx.HdmiRx.SLAVE_ADDRESSES[0],
      ids.VGA: rx.VgaRx.SLAVE_ADDRESSES[0]
  }
  _MUX_CONFIGS = {
      # Use a dual-pixel-mode setting for IO as no support for two flows
      # simultaneously so far.
      ids.DP1: io.MuxIo.CONFIG_DP1_DUAL,
      ids.DP2: io.MuxIo.CONFIG_DP2_DUAL,
      ids.HDMI: io.MuxIo.CONFIG_HDMI_DUAL,
      ids.VGA: io.MuxIo.CONFIG_VGA
  }

  def __init__(self, input_id, main_i2c_bus, fpga_ctrl):
    """Constructs a InputFlow object.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
      main_i2c_bus: The main I2cBus object.
      fpga_ctrl: The FpgaController object.
    """
    self._input_id = input_id
    self._main_bus = main_i2c_bus
    self._fpga = fpga_ctrl
    self._power_io = self._main_bus.GetSlave(io.PowerIo.SLAVE_ADDRESSES[0])
    self._mux_io = self._main_bus.GetSlave(io.MuxIo.SLAVE_ADDRESSES[0])
    self._rx = self._main_bus.GetSlave(self._RX_SLAVES[self._input_id])
    self._frame_manager = frame_manager.FrameManager(
        input_id, self._rx, self._GetEffectiveVideoDumpers())
    self._edid = None  # be overwitten by a subclass.
    self._edid_enabled = True
    self._ddc_enabled = True

  def _GetEffectiveVideoDumpers(self):
    """Gets effective video dumpers on the flow."""
    if self.IsDualPixelMode():
      if fpga.VideoDumper.EVEN_PIXELS_FLOW_INDEXES[self._input_id] == 0:
        return [self._fpga.vdump0, self._fpga.vdump1]
      else:
        return [self._fpga.vdump1, self._fpga.vdump0]
    elif fpga.VideoDumper.PRIMARY_FLOW_INDEXES[self._input_id] == 0:
      return [self._fpga.vdump0]
    else:
      return [self._fpga.vdump1]

  def Initialize(self):
    """Initializes the input flow."""
    logging.info('Initialize InputFlow #%d.', self._input_id)
    self._power_io.ResetReceiver(self._input_id)
    self._rx.Initialize(self.IsDualPixelMode())

  def Select(self):
    """Selects the input flow to set the proper muxes and FPGA paths."""
    logging.info('Select InputFlow #%d.', self._input_id)
    self._mux_io.SetConfig(self._MUX_CONFIGS[self._input_id])
    self._fpga.vpass.Select(self._input_id)
    self._fpga.vdump0.Select(self._input_id, self.IsDualPixelMode())
    self._fpga.vdump1.Select(self._input_id, self.IsDualPixelMode())

  def GetPixelDumpArgs(self):
    """Gets the arguments of pixeldump tool which selects the proper buffers."""
    return fpga.VideoDumper.GetPixelDumpArgs(self._input_id,
                                             self.IsDualPixelMode())

  @classmethod
  def GetConnectorType(cls):
    """Returns the human readable string for the connector type."""
    return cls._CONNECTOR_TYPE

  def GetMaxFrameLimit(self, width, height):
    """Returns of the maximal number of frames which can be dumped."""
    return self._frame_manager.GetMaxFrameLimit(width, height)

  def GetFrameHashes(self, start, stop):
    """Returns the list of the frame hashes.

    Args:
      start: The index of the start frame.
      stop: The index of the stop frame (excluded).

    Returns:
      A list of frame hashes.
    """
    return self._frame_manager.GetFrameHashes(start, stop)

  def DumpFramesToLimit(self, frame_limit, x, y, width, height, timeout):
    """Dumps frames and waits for the given limit being reached or timeout.

    Args:
      frame_limit: The limitation of frame to dump.
      x: The X position of the top-left corner of crop; None for a full-screen.
      y: The Y position of the top-left corner of crop; None for a full-screen.
      width: The width of the area of crop.
      height: The height of the area of crop.
      timeout: Time in second of timeout.

    Raises:
      common.TimeoutError on timeout.
    """
    self.WaitVideoOutputStable()
    try:
      self._frame_manager.DumpFramesToLimit(frame_limit, x, y, width, height,
                                            timeout)
    except common.TimeoutError:
      message = 'Frames failed to reach %d' % frame_limit
      logging.error(message)
      logging.error('RX dump: %r', self._rx.Get(0, 256))
      raise InputFlowError(message)

  def StartDumpingFrames(self, frame_buffer_limit, x, y, width, height,
                         hash_buffer_limit):
    """Starts dumping frames continuously.

    Args:
      frame_buffer_limit: The size of the buffer which stores the frame.
                          Frames will be dumped to the beginning when full.
      x: The X position of the top-left corner of crop; None for a full-screen.
      y: The Y position of the top-left corner of crop; None for a full-screen.
      width: The width of the area of crop.
      height: The height of the area of crop.
      hash_buffer_limit: The maximum number of hashes to monitor. Stop
                         capturing when this limitation is reached.
    """
    self.WaitVideoOutputStable()
    self._frame_manager.StartDumpingFrames(
        frame_buffer_limit, x, y, width, height, hash_buffer_limit)

  def StopDumpingFrames(self):
    """Stops dumping frames."""
    self._frame_manager.StopDumpingFrames()

  def GetDumpedFrameCount(self):
    """Gets the number of frames which is dumped."""
    return self._frame_manager.GetFrameCount()

  def Do_FSM(self):
    """Does the Finite-State-Machine to ensure the input flow ready.

    The receiver requires to do the FSM in order to clear its state, in case
    of some events happended, like mode change, power reattach, etc.

    It should be called before doing any post-receiver-action, like capturing
    frames.
    """
    pass

  def WaitVideoInputStable(self, unused_timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    return True

  def WaitVideoOutputStable(self, unused_timeout=None):
    """Waits the video output stable or timeout.

    Raises:
      InputFlowError if timeout.
    """
    pass

  def IsDualPixelMode(self):
    """Returns if the input flow uses dual pixel mode."""
    raise NotImplementedError('IsDualPixelMode')

  def IsPhysicalPlugged(self):
    """Returns if the physical cable is plugged."""
    raise NotImplementedError('IsPhysicalPlugged')

  def IsPlugged(self):
    """Returns if the HPD line is plugged."""
    raise NotImplementedError('IsPlugged')

  def Plug(self):
    """Asserts HPD line to high, emulating plug."""
    raise NotImplementedError('Plug')

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    raise NotImplementedError('Unplug')

  def SetEdidState(self, enabled):
    """Sets the enabled/disabled state of EDID.

    Args:
      enabled: True to enable EDID due to an user request; False to
               disable it.
    """
    if enabled and self.IsPlugged():
      self._edid.Enable()
    else:
      self._edid.Disable()
    self._edid_enabled = enabled

  def IsEdidEnabled(self):
    """Checks if the EDID is enabled or disabled.

    Returns:
      True if the EDID is enabled; False if disabled.
    """
    return self._edid_enabled

  def FireHpdPulse(
      self, deassert_interval_usec, assert_interval_usec, repeat_count,
      end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    raise NotImplementedError('FireHpdPulse')

  def FireMixedHpdPulses(self, widths_msec):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths_msec argument where
    widths_msec[0] is the width of the first low segment, widths_msec[1] is that
    of the first high segment, widths_msec[2] is that of the second low segment,
    etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    The method is equivalent to a series of calls to Unplug() and Plug()
    separated by specified pulse widths.

    Args:
      widths_msec: list of pulse segment widths in milli-second.
    """
    # Append a plug/unplug after the last pulse
    sleep_times = [w / 1000.0 for w in widths_msec] + [0.0]
    ops = [self.Unplug, self.Plug] * ((len(sleep_times) + 1) / 2)
    pulses = itertools.izip(ops, sleep_times)

    for op, sleep_time in pulses:
      op()
      time.sleep(sleep_time)

  def _EnableDdc(self):
    """Enables the DDC bus."""
    raise NotImplementedError('EnableDdc')

  def _DisableDdc(self):
    """Disables the DDC bus."""
    raise NotImplementedError('DisableDdc')

  def SetDdcState(self, enabled):
    """Sets the enabled/disabled state of DDC bus.

    Args:
      enabled: True to enable DDC bus due to an user request; False to
              disable it.
    """
    if enabled and self.IsPlugged():
      self._EnableDdc()
    else:
      self._DisableDdc()
    self._ddc_enabled = enabled

  def IsDdcEnabled(self):
    """Checks if the DDC bus is enabled or disabled.

    Returns:
      True if the DDC bus is enabled; False if disabled.
    """
    return self._ddc_enabled

  def ReadEdid(self):
    """Reads the EDID content."""
    raise NotImplementedError('ReadEdid')

  def WriteEdid(self, data):
    """Writes the EDID content."""
    raise NotImplementedError('WriteEdid')

  def SetContentProtection(self, enabled):
    """Sets the content protection state.

    Args:
      enabled: True to enable; False to disable.
    """
    raise NotImplementedError('SetContentProtection')

  def IsContentProtectionEnabled(self):
    """Returns True if the content protection is enabled.

    Returns:
      True if the content protection is enabled; otherwise, False.
    """
    raise NotImplementedError('IsContentProtectionEnabled')

  def IsVideoInputEncrypted(self):
    """Returns True if the video input is encrypted.

    Returns:
      True if the video input is encrypted; otherwise, False.
    """
    raise NotImplementedError('IsVideoInputEncrypted')


class InputFlowWithAudio(InputFlow):  # pylint: disable=W0223
  """An abstraction of an input flow which supports audio."""

  def __init__(self, input_id, main_i2c_bus, fpga_ctrl):
    """Constructs a InputFlowWithAudio object.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
      main_i2c_bus: The main I2cBus object.
      fpga_ctrl: The FpgaController object.
    """
    super(InputFlowWithAudio, self).__init__(input_id, main_i2c_bus, fpga_ctrl)
    self._audio_capture_manager = audio_utils.AudioCaptureManager(
        self._fpga.adump)
    self._audio_route_manager = audio_utils.AudioRouteManager(
        self._fpga.aroute)

  @property
  def is_capturing_audio(self):
    """Is input flow capturing audio?"""
    return self._audio_capture_manager.is_capturing

  def StartCapturingAudio(self):
    """Starts capturing audio."""
    self._audio_route_manager.SetupRouteFromInputToDumper(self._input_id)
    self._audio_capture_manager.StartCapturingAudio()

  def StopCapturingAudio(self):
    """Stops capturing audio.

    Returns:
      A tuple (data, format).
      data: The captured audio data.
      format: The dict representation of AudioDataFormat. Refer to docstring
        of utils.audio.AudioDataFormat for detail.

    Raises:
      AudioCaptureManagerError: If captured time or page exceeds the limit.
      AudioCaptureManagerError: If there is no captured data.
    """
    return_value = self._audio_capture_manager.StopCapturingAudio()
    self.ResetRoute()
    return return_value

  def ResetRoute(self):
    """Resets the audio route."""
    self._audio_route_manager.ResetRouteToDumper()


class DpInputFlow(InputFlow):
  """An abstraction of the entire flow for DisplayPort."""

  _CONNECTOR_TYPE = 'DP'
  _IS_DUAL_PIXEL_MODE = False

  _DELAY_VIDEO_MODE_PROBE = 1.0
  _TIMEOUT_VIDEO_STABLE_PROBE = 5

  _HPD_PULSE_WIDTH = 0.1

  _AUX_BYPASS_MUXES = {
      ids.DP1: io.MuxIo.MASK_DP1_AUX_BP_L,
      ids.DP2: io.MuxIo.MASK_DP2_AUX_BP_L
  }

  def __init__(self, *args):
    super(DpInputFlow, self).__init__(*args)
    self._edid = edid.DpEdid(args[0], self._main_bus)

  def IsDualPixelMode(self):
    """Returns if the input flow uses dual pixel mode."""
    return self._IS_DUAL_PIXEL_MODE

  def IsPhysicalPlugged(self):
    """Returns if the physical cable is plugged."""
    return self._rx.IsCablePowered()

  def IsPlugged(self):
    """Returns if the HPD line is plugged."""
    return self._fpga.hpd.IsPlugged(self._input_id)

  def Plug(self):
    """Asserts HPD line to high, emulating plug."""
    if self.IsEdidEnabled():
      self._edid.Enable()
    if self.IsDdcEnabled():
      self._EnableDdc()
    self._fpga.hpd.Plug(self._input_id)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    self._edid.Disable()
    self._DisableDdc()
    self._fpga.hpd.Unplug(self._input_id)

  def FireHpdPulse(
      self, deassert_interval_usec, assert_interval_usec, repeat_count,
      end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    self._fpga.hpd.FireHpdPulse(
        self._input_id, deassert_interval_usec, assert_interval_usec,
        repeat_count, end_level)

  def _EnableDdc(self):
    """Enable the DDC bus."""
    # Enable AUX bypass
    self._mux_io.ClearOutputMask(self._AUX_BYPASS_MUXES[self._input_id])

  def _DisableDdc(self):
    """Disable the DDC bus."""
    # Disable AUX bypass
    self._mux_io.SetOutputMask(self._AUX_BYPASS_MUXES[self._input_id])

  def ReadEdid(self):
    """Reads the EDID content."""
    return self._edid.ReadEdid()

  def WriteEdid(self, data):
    """Writes the EDID content."""
    self._edid.WriteEdid(data)

  def WaitVideoInputStable(self, timeout=None):
    """Waits the video input stable or timeout."""
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE

    try:
      common.WaitForCondition(
          self._rx.IsVideoInputStable, True, self._DELAY_VIDEO_MODE_PROBE,
          timeout)
      return True
    except common.TimeoutError:
      return False

  def _IsFrameLocked(self):
    """Returns whether the FPGA frame is locked.

    It compares the resolution reported from the receiver with the FPGA.

    Returns:
      True if the frame is locked; otherwise, False.
    """
    resolution_fpga = self._frame_manager.ComputeResolution()
    resolution_rx = self._rx.GetFrameResolution()
    if resolution_fpga == resolution_rx:
      logging.info('same resolution: %dx%d', *resolution_fpga)
      return True
    else:
      logging.info('diff resolution: fpga:%dx%d != rx:%dx%d',
                   *(resolution_fpga + resolution_rx))
      return False

  def WaitVideoOutputStable(self, timeout=None):
    """Waits the video output stable or timeout."""
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE
    try:
      common.WaitForCondition(
          self._IsFrameLocked, True, self._DELAY_VIDEO_MODE_PROBE, timeout)
    except common.TimeoutError:
      return False
    return True

  def GetResolution(self):
    """Gets the resolution of the video flow."""
    if self.WaitVideoOutputStable():
      return self._rx.GetFrameResolution()
    else:
      raise InputFlowError(
          'Frame resolution not stable. Rx:%r, FPGA:%r',
          self._rx.GetFrameResolution(),
          self._frame_manager.ComputeResolution())

  def Do_FSM(self):
    """Does the Finite-State-Machine to ensure the input flow ready.

    The receiver requires to do the FSM in order to clear its state, in case
    of some events happended, like mode change, power reattach, etc.

    It should be called before doing any post-receiver-action, like capturing
    frames.
    """
    if not self._rx.IsVideoInputStable() or not self._IsFrameLocked():
      self._rx.ResetVideoLogic()
      if not self.WaitVideoInputStable() or not self.WaitVideoOutputStable():
        logging.info('Send DP HPD pulse to reset source...')
        self._fpga.hpd.Unplug(self._input_id)
        time.sleep(self._HPD_PULSE_WIDTH)
        self._fpga.hpd.Plug(self._input_id)
        if self.WaitVideoInputStable() and self.WaitVideoOutputStable():
          logging.info('DP FSM done')
        else:
          logging.error('*** DP FSM failed')
    else:
      logging.info('Skip resetting DP rx.')

  def SetContentProtection(self, enabled):
    """Sets the content protection state.

    Args:
      enabled: True to enable; False to disable.
    """
    raise NotImplementedError('SetContentProtection')

  def IsContentProtectionEnabled(self):
    """Returns True if the content protection is enabled.

    Returns:
      True if the content protection is enabled; otherwise, False.
    """
    raise NotImplementedError('IsContentProtectionEnabled')

  def IsVideoInputEncrypted(self):
    """Returns True if the video input is encrypted.

    Returns:
      True if the video input is encrypted; otherwise, False.
    """
    raise NotImplementedError('IsVideoInputEncrypted')


class HdmiInputFlow(InputFlowWithAudio):
  """An abstraction of the entire flow for HDMI."""

  _CONNECTOR_TYPE = 'HDMI'

  # The firmware for the 6803 reference board sets the rx in dual pixel mode
  # when the pixel clock is greater than 160. Here, we use 125 instead of 160
  # as the FPGA works more reliably when the pixel clock is under this value.
  # Two thresholds defining a hysteresis zone to avoid rapid mode changes due
  # to pixel clock noise.
  _PIXEL_MODE_PCLK_THRESHOLD_HIGH = 125 # MHz
  _PIXEL_MODE_PCLK_THRESHOLD_LOW = 115 # MHz

  _DELAY_VIDEO_MODE_PROBE = 0.1
  _TIMEOUT_VIDEO_STABLE_PROBE = 10
  _DELAY_WAITING_GOOD_PIXELS = 3

  def __init__(self, *args):
    self._is_dual_pixel_mode = True

    super(HdmiInputFlow, self).__init__(*args)
    self._edid = edid.HdmiEdid(self._main_bus)

  def IsDualPixelMode(self):
    """Returns if the input flow uses dual pixel mode."""
    return self._is_dual_pixel_mode

  def IsPhysicalPlugged(self):
    """Returns if the physical cable is plugged."""
    return self._rx.IsCablePowered()

  def IsPlugged(self):
    """Returns if the HPD line is plugged."""
    return self._fpga.hpd.IsPlugged(self._input_id)

  def Plug(self):
    """Asserts HPD line to high, emulating plug."""
    if self.IsEdidEnabled():
      self._edid.Enable()
    if self.IsDdcEnabled():
      self._EnableDdc()
    self._fpga.hpd.Plug(self._input_id)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    self._edid.Disable()
    self._DisableDdc()
    self._fpga.hpd.Unplug(self._input_id)

  def FireHpdPulse(
      self, deassert_interval_usec, assert_interval_usec, repeat_count,
      end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    self._fpga.hpd.FireHpdPulse(
        self._input_id, deassert_interval_usec, assert_interval_usec,
        repeat_count, end_level)

  def _EnableDdc(self):
    """Enable the DDC bus."""
    self._mux_io.ClearOutputMask(io.MuxIo.MASK_HDMI_DDC_BP_L)

  def _DisableDdc(self):
    """Disable the DDC bus."""
    self._mux_io.SetOutputMask(io.MuxIo.MASK_HDMI_DDC_BP_L)

  def ReadEdid(self):
    """Reads the EDID content."""
    return self._edid.ReadEdid()

  def WriteEdid(self, data):
    """Writes the EDID content."""
    self._edid.WriteEdid(data)

  def _SetPixelMode(self):
    """Sets the pixel mode based on the pixel clock of the input signal.

    Returns:
      True if pixel mode is changed; False if nothing is changed.
    """
    pclk = self._rx.GetPixelClock()
    logging.info('PCLK = %s', pclk)
    if (self._PIXEL_MODE_PCLK_THRESHOLD_LOW <= pclk <=
        self._PIXEL_MODE_PCLK_THRESHOLD_HIGH):
      # Hysteresis: do not change mode if pclk is in this buffer zone.
      return False
    dual_pixel_mode = pclk >= self._PIXEL_MODE_PCLK_THRESHOLD_HIGH
    if self._is_dual_pixel_mode != dual_pixel_mode:
      self._is_dual_pixel_mode = dual_pixel_mode
      if dual_pixel_mode:
        self._rx.SetDualPixelMode()
        logging.info('Changed to dual pixel mode')
      else:
        self._rx.SetSinglePixelMode()
        logging.info('Changed to single pixel mode')
      self._frame_manager = frame_manager.FrameManager(
          self._input_id, self._rx, self._GetEffectiveVideoDumpers())
      self.Select()
      return True
    return False

  def Do_FSM(self):
    """Does the Finite-State-Machine to ensure the input flow ready.

    The receiver requires to do the FSM in order to clear its state, in case
    of some events happended, like mode change, power reattach, etc.

    It should be called before doing any post-receiver-action, like capturing
    frames.
    """
    is_reset_needed = self._rx.IsResetNeeded()
    if is_reset_needed:
      self._rx.Reset()

    if self.WaitVideoInputStable():
      pixel_mode_changed = self._SetPixelMode()
      if is_reset_needed or pixel_mode_changed:
        self.WaitVideoOutputStable()
        # TODO(waihong): Remove this hack only for Nyan-Big.
        # http://crbug.com/402152
        time.sleep(self._DELAY_WAITING_GOOD_PIXELS)
    else:
      message = 'Video input not stable.'
      logging.error(message)
      raise InputFlowError(message)

  def WaitVideoInputStable(self, timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE
    try:
      common.WaitForCondition(
          self._rx.IsVideoInputStable, True, self._DELAY_VIDEO_MODE_PROBE,
          timeout)
    except common.TimeoutError:
      return False
    return True

  def _IsFrameLocked(self):
    """Returns whether the FPGA frame is locked.

    It compares the resolution reported from the receiver with the FPGA.

    Returns:
      True if the frame is locked; otherwise, False.
    """
    resolution_fpga = self._frame_manager.ComputeResolution()
    resolution_rx = self._rx.GetFrameResolution()
    if resolution_fpga == resolution_rx:
      logging.info('same resolution: %dx%d', *resolution_fpga)
      return True
    else:
      logging.info('diff resolution: fpga:%dx%d != rx:%dx%d',
                   *(resolution_fpga + resolution_rx))
      return False

  def WaitVideoOutputStable(self, timeout=None):
    """Waits the video output stable or timeout.

    Raises:
      InputFlowError if timeout.
    """
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE
    try:
      common.WaitForCondition(
          self._IsFrameLocked, True, self._DELAY_VIDEO_MODE_PROBE, timeout)
    except common.TimeoutError:
      message = 'Timeout waiting video output stable'
      logging.error(message)
      logging.error('RX dump: %r', self._rx.Get(0, 256))
      raise InputFlowError(message)

  def GetResolution(self):
    """Gets the resolution of the video flow."""
    self.WaitVideoOutputStable()
    return self._rx.GetFrameResolution()

  def SetContentProtection(self, enabled):
    """Sets the content protection state.

    Args:
      enabled: True to enable; False to disable.
    """
    self._rx.SetContentProtection(enabled)

  def IsContentProtectionEnabled(self):
    """Returns True if the content protection is enabled.

    Returns:
      True if the content protection is enabled; otherwise, False.
    """
    return self._rx.IsContentProtectionEnabled()

  def IsVideoInputEncrypted(self):
    """Returns True if the video input is encrypted.

    Returns:
      True if the video input is encrypted; otherwise, False.
    """
    return self._rx.IsVideoInputEncrypted()


class VgaInputFlow(InputFlow):
  """An abstraction of the entire flow for VGA."""

  _CONNECTOR_TYPE = 'VGA'
  _IS_DUAL_PIXEL_MODE = False
  _DELAY_CHECKING_STABLE_PROBE = 0.1
  _TIMEOUT_CHECKING_STABLE = 5
  _DELAY_RESOLUTION_PROBE = 0.05

  def __init__(self, *args):
    super(VgaInputFlow, self).__init__(*args)
    self._edid = edid.VgaEdid(self._fpga)
    self._auto_vga_mode = True

  def IsDualPixelMode(self):
    """Returns if the input flow uses dual pixel mode."""
    return self._IS_DUAL_PIXEL_MODE

  def IsPhysicalPlugged(self):
    """Returns if the physical cable is plugged."""
    # VGA has no HPD to detect hot-plug. We check the source signal
    # to make that decision. So plug it and wait a while to see any
    # signal received. It does not work if DUT is not well-behaved.
    plugged_before_check = self.IsPlugged()
    if not plugged_before_check:
      self.Plug()
    is_stable = self.WaitVideoInputStable()
    if not plugged_before_check:
      self.Unplug()
    return is_stable

  def IsPlugged(self):
    """Returns if the HPD line is plugged."""
    return not bool(self._mux_io.GetOutput() & io.MuxIo.MASK_VGA_BLOCK_SOURCE)

  def Plug(self):
    """Asserts HPD line to high, emulating plug."""
    if self.IsEdidEnabled():
      self._edid.Enable()
    if self.IsDdcEnabled():
      self._EnableDdc()
    # For VGA, unblock the RGB source to emulate plug.
    self._mux_io.ClearOutputMask(io.MuxIo.MASK_VGA_BLOCK_SOURCE)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    self._edid.Disable()
    self._DisableDdc()
    # For VGA, block the RGB source to emulate unplug.
    self._mux_io.SetOutputMask(io.MuxIo.MASK_VGA_BLOCK_SOURCE)

  def FireHpdPulse(
      self, deassert_interval_usec, assert_interval_usec, repeat_count,
      end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    pass

  def FireMixedHpdPulses(self, widths_msec):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths_msec argument where
    widths_msec[0] is the width of the first low segment, widths_msec[1] is that
    of the first high segment, widths_msec[2] is that of the second low segment,
    etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    The method is equivalent to a series of calls to Unplug() and Plug()
    separated by specified pulse widths.

    Args:
      widths_msec: list of pulse segment widths in milli-second.
    """
    pass

  def SetVgaMode(self, mode):
    """Sets the mode for VGA monitor."""
    if mode.lower() == 'auto':
      self._auto_vga_mode = True
    else:
      self._auto_vga_mode = False
      self._rx.SetMode(mode)

  def _EnableDdc(self):
    """Enable the DDC bus."""
    # Chameleon board does not support disabling the DDC bus on VGA.
    # Simply enable the EDID.
    self._edid.Enable()

  def _DisableDdc(self):
    """Disable the DDC bus."""
    # Chameleon board does not support disabling the DDC bus on VGA.
    # Simply disable the EDID.
    self._edid.Disable()

  def ReadEdid(self):
    """Reads the EDID content."""
    return self._edid.ReadEdid()

  def WriteEdid(self, data):
    """Writes the EDID content."""
    self._edid.WriteEdid(data)

  def Do_FSM(self):
    """Does the Finite-State-Machine to ensure the input flow ready.

    The receiver requires to do the FSM in order to clear its state, in case
    of some events happended, like mode change, power reattach, etc.

    It should be called before doing any post-receiver-action, like capturing
    frames.
    """
    if self._auto_vga_mode:
      # Detect the VGA mode and set it properly.
      if self.WaitVideoInputStable():
        self._rx.SetMode(self._rx.DetectMode())
        self.WaitVideoOutputStable()
      else:
        logging.warn('Skip doing receiver FSM as video input not stable.')

  def WaitVideoInputStable(self, timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_CHECKING_STABLE
    try:
      # Check if H-Sync/V-Sync recevied from the source.
      common.WaitForCondition(
          self._rx.IsSyncDetected, True, self._DELAY_CHECKING_STABLE_PROBE,
          timeout)
    except common.TimeoutError:
      return False
    return True

  def _IsResolutionValid(self):
    """Returns True if the resolution from FPGA is valid and not floating."""
    resolution1 = self._frame_manager.ComputeResolution()
    time.sleep(self._DELAY_RESOLUTION_PROBE)
    resolution2 = self._frame_manager.ComputeResolution()
    return resolution1 == resolution2 and 0 not in resolution1

  def WaitVideoOutputStable(self, timeout=None):
    """Waits the video output stable or timeout.

    Raises:
      InputFlowError if timeout.
    """
    if timeout is None:
      timeout = self._TIMEOUT_CHECKING_STABLE
    try:
      # Wait a valid resolution and not floating.
      common.WaitForCondition(
          self._IsResolutionValid, True, self._DELAY_CHECKING_STABLE_PROBE,
          timeout)
    except common.TimeoutError:
      message = 'Timeout waiting video output stable'
      logging.error(message)
      logging.error('RX dump: %r', self._rx.Get(0, 256))
      raise InputFlowError(message)

  def GetResolution(self):
    """Gets the resolution of the video flow."""
    self.WaitVideoOutputStable()
    width, height = self._frame_manager.ComputeResolution()
    if width == 0 or height == 0:
      raise InputFlowError('Something wrong with the resolution: %dx%d' %
                           (width, height))
    return (width, height)

  def SetContentProtection(self, enabled):
    """Sets the content protection state.

    Args:
      enabled: True to enable; False to disable.
    """
    raise InputFlowError('VGA not support content protection')

  def IsContentProtectionEnabled(self):
    """Returns True if the content protection is enabled.

    Returns:
      True if the content protection is enabled; otherwise, False.
    """
    # VGA not support content protection.
    return False

  def IsVideoInputEncrypted(self):
    """Returns True if the video input is encrypted.

    Returns:
      True if the video input is encrypted; otherwise, False.
    """
    # VGA not support content protection.
    return False
