# Copyright (c) 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Input flow module which abstracts the entire flow for a specific input."""

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
        input_id, self._GetEffectiveVideoDumpers())

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
    self.WaitVideoOutputStable()

  def GetPixelDumpArgs(self):
    """Gets the arguments of pixeldump tool which selects the proper buffers."""
    return fpga.VideoDumper.GetPixelDumpArgs(self._input_id,
                                             self.IsDualPixelMode())

  @classmethod
  def GetConnectorType(cls):
    """Returns the human readable string for the connector type."""
    return cls._CONNECTOR_TYPE

  def GetResolution(self):
    """Gets the resolution of the video flow."""
    self.WaitVideoOutputStable()
    width, height = self._frame_manager.ComputeResolution()
    if width == 0 or height == 0:
      raise InputFlowError('Something wrong with the resolution: %dx%d' %
                           (width, height))
    return (width, height)

  def GetMaxFrameLimit(self, width, height):
    """Returns of the maximal number of frames which can be dumped."""
    if self.IsDualPixelMode():
      width = width / 2
    return fpga.VideoDumper.GetMaxFrameLimit(width, height)

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
    self._frame_manager.DumpFramesToLimit(frame_limit, x, y, width, height,
                                          timeout)

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
    """Waits the video output stable or timeout. Returns success or not."""
    return True

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

  def FireHpdPulse(self, deassert_interval_usec, assert_interval_usec,
          repeat_count, end_level):
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

  def FireMixedHpdPulses(self, widths):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths argument where
    widths[0] is the width of the first low segment, widths[1] is that of the
    first high segment, widths[2] is that of the second low segment, ... etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    Args:
      widths: list of pulse segment widths in usec.
    """
    raise NotImplementedError('FireMixedHpdPulses')

  def ReadEdid(self):
    """Reads the EDID content."""
    raise NotImplementedError('ReadEdid')

  def WriteEdid(self, data):
    """Writes the EDID content."""
    raise NotImplementedError('WriteEdid')


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

  def Select(self):
    """Selects the input flow to set the proper muxes and FPGA paths."""
    super(InputFlowWithAudio, self).Select()
    self._fpga.aroute.SetupRouteFromInputToDumper(self._input_id)

  @property
  def is_capturing_audio(self):
    """Is input flow capturing audio?"""
    return self._audio_capture_manager.is_capturing

  def StartCapturingAudio(self):
    """Starts capturing audio."""
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
    return self._audio_capture_manager.StopCapturingAudio()


class DpInputFlow(InputFlow):
  """An abstraction of the entire flow for DisplayPort."""

  _CONNECTOR_TYPE = 'DP'
  _IS_DUAL_PIXEL_MODE = False

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
    self._edid.Enable()
    self._fpga.hpd.Plug(self._input_id)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    self._fpga.hpd.Unplug(self._input_id)
    self._edid.Disable()

  def FireHpdPulse(self, deassert_interval_usec, assert_interval_usec,
          repeat_count, end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    self._fpga.hpd.FireHpdPulse(self._input_id, deassert_interval_usec,
            assert_interval_usec, repeat_count, end_level)

  def FireMixedHpdPulses(self, widths):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths argument where
    widths[0] is the width of the first low segment, widths[1] is that of the
    first high segment, widths[2] is that of the second low segment, ... etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    Args:
      widths: list of pulse segment widths in usec.
    """
    self._fpga.hpd.FireMixedHpdPulses(self._input_id, widths)

  def ReadEdid(self):
    """Reads the EDID content."""
    return self._edid.ReadEdid()

  def WriteEdid(self, data):
    """Writes the EDID content."""
    self._edid.WriteEdid(data)

  def WaitVideoInputStable(self, unused_timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    # TODO(waihong): Implement this method.
    return True

  def WaitVideoOutputStable(self, unused_timeout=None):
    """Waits the video output stable or timeout. Returns success or not."""
    # TODO(waihong): Implement this method.
    return True


class HdmiInputFlow(InputFlowWithAudio):
  """An abstraction of the entire flow for HDMI."""

  _CONNECTOR_TYPE = 'HDMI'
  _IS_DUAL_PIXEL_MODE = True

  _DELAY_VIDEO_MODE_PROBE = 0.1
  _TIMEOUT_VIDEO_STABLE_PROBE = 10
  _DELAY_WAITING_GOOD_PIXELS = 3

  def __init__(self, *args):
    super(HdmiInputFlow, self).__init__(*args)
    self._edid = edid.HdmiEdid(self._main_bus)

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
    self._edid.Enable()
    self._fpga.hpd.Plug(self._input_id)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    self._fpga.hpd.Unplug(self._input_id)
    self._edid.Disable()

  def FireHpdPulse(self, deassert_interval_usec, assert_interval_usec,
          repeat_count, end_level):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    self._fpga.hpd.FireHpdPulse(self._input_id, deassert_interval_usec,
            assert_interval_usec, repeat_count, end_level)

  def FireMixedHpdPulses(self, widths):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths argument where
    widths[0] is the width of the first low segment, widths[1] is that of the
    first high segment, widths[2] is that of the second low segment, ... etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    Args:
      widths: list of pulse segment widths in usec.
    """
    self._fpga.hpd.FireMixedHpdPulses(self._input_id, widths)

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
    if self.WaitVideoInputStable():
      if self._rx.IsResetNeeded():
        self._rx.Reset()
        self.WaitVideoOutputStable()
        # TODO(waihong): Remove this hack only for Nyan-Big.
        # http://crbug.com/402152
        time.sleep(self._DELAY_WAITING_GOOD_PIXELS)
    else:
      logging.warn('Skip doing receiver FSM as video input not stable.')

  def WaitVideoInputStable(self, timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE
    try:
      common.WaitForCondition(self._rx.IsVideoInputStable, True,
          self._DELAY_VIDEO_MODE_PROBE, timeout)
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
    resolution_rx = self._rx.GetResolution()
    if resolution_fpga == resolution_rx:
      logging.info('same resolution: %dx%d', *resolution_fpga)
      return True
    else:
      logging.info('diff resolution: fpga:%dx%d != rx:%dx%d',
                   *(resolution_fpga + resolution_rx))
      return False

  def WaitVideoOutputStable(self, timeout=None):
    """Waits the video output stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_VIDEO_STABLE_PROBE
    try:
      common.WaitForCondition(self._IsFrameLocked, True,
          self._DELAY_VIDEO_MODE_PROBE, timeout)
    except common.TimeoutError:
      return False
    return True


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
    self._edid.Enable()
    # For VGA, unblock the RGB source to emulate plug.
    self._mux_io.ClearOutputMask(io.MuxIo.MASK_VGA_BLOCK_SOURCE)

  def Unplug(self):
    """Deasserts HPD line to low, emulating unplug."""
    # For VGA, block the RGB source to emulate unplug.
    self._mux_io.SetOutputMask(io.MuxIo.MASK_VGA_BLOCK_SOURCE)
    self._edid.Disable()

  def FireHpdPulse(self, deassert_interval_usec, assert_interval_usec,
          repeat_count, end_level):
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

  def FireMixedHpdPulses(self, widths):
    """Fires one or more HPD pulses, starting at low, of mixed widths.

    One must specify a list of segment widths in the widths argument where
    widths[0] is the width of the first low segment, widths[1] is that of the
    first high segment, widths[2] is that of the second low segment, ... etc.
    The HPD line stops at low if even number of segment widths are specified;
    otherwise, it stops at high.

    Args:
      widths: list of pulse segment widths in usec.
    """
    pass

  def ReadEdid(self):
    """Reads the EDID content."""
    return self._edid.ReadEdid()

  def WriteEdid(self, data):
    """Writes the EDID content."""
    self._edid.WriteEdid(data)

  def WaitVideoInputStable(self, timeout=None):
    """Waits the video input stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_CHECKING_STABLE
    try:
      # Check if H-Sync/V-Sync recevied from the source.
      common.WaitForCondition(self._rx.IsSyncDetected, True,
          self._DELAY_CHECKING_STABLE_PROBE, timeout)
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
    """Waits the video output stable or timeout. Returns success or not."""
    if timeout is None:
      timeout = self._TIMEOUT_CHECKING_STABLE
    try:
      # Wait a valid resolution and not floating.
      common.WaitForCondition(self._IsResolutionValid, True,
          self._DELAY_CHECKING_STABLE_PROBE, timeout)
    except common.TimeoutError:
      return False
    return True
