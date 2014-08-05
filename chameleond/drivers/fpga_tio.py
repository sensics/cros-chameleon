# Copyright (c) 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Chameleond Driver for FPGA customized platform with the TIO card."""

import logging
import os
import tempfile
import xmlrpclib

import chameleon_common  # pylint: disable=W0611
from chameleond.interface import ChameleondInterface

from chameleond.utils import fpga
from chameleond.utils import i2c_fpga as i2c
from chameleond.utils import ids
from chameleond.utils import input_flow
from chameleond.utils import system_tools


class DriverError(Exception):
  """Exception raised when any error on FPGA driver."""
  pass


class ChameleondDriver(ChameleondInterface):
  """Chameleond Driver for FPGA customized platform."""

  _I2C_BUS_MAIN = 0

  _PIXEL_FORMAT = 'rgb'

  # Time to wait for video frame dump to start before a timeout error is raised
  _TIMEOUT_FRAME_DUMP_PROBE = 60.0

  # The frame index which is used for the regular DumpPixels API.
  _DEFAULT_FRAME_INDEX = 0
  _DEFAULT_FRAME_LIMIT = _DEFAULT_FRAME_INDEX + 1

  # Inputs that support audio.
  _INPUTS_AUDIO_SUPPORTED = [ids.HDMI]

  def __init__(self, *args, **kwargs):
    super(ChameleondDriver, self).__init__(*args, **kwargs)
    self._selected_input = None
    self._captured_params = {}
    # Reserve index 0 as the default EDID.
    self._all_edids = [self._ReadDefaultEdid()]

    self._tools = system_tools.SystemTools
    main_bus = i2c.I2cBus(self._I2C_BUS_MAIN)
    fpga_ctrl = fpga.FpgaController()
    self._input_flows = {
      ids.DP1: input_flow.DpInputFlow(ids.DP1, main_bus, fpga_ctrl),
      ids.DP2: input_flow.DpInputFlow(ids.DP2, main_bus, fpga_ctrl),
      ids.HDMI: input_flow.HdmiInputFlow(ids.HDMI, main_bus, fpga_ctrl),
      ids.VGA: input_flow.VgaInputFlow(ids.VGA, main_bus, fpga_ctrl)
    }

    for flow in self._input_flows.itervalues():
      if flow:
        flow.Initialize()

    self.Reset()

    # Set all ports unplugged on initialization.
    for input_id in self.ProbeInputs():
      self.Unplug(input_id)


  def Reset(self):
    """Resets Chameleon board."""
    logging.info('Execute the reset process.')
    # TODO(waihong): Add other reset routines.
    self._ApplyDefaultEdid()


  def IsHealthy(self):
    """Returns if the Chameleon is healthy or any repair is needed.

    Returns:
      True if the Chameleon is healthy; otherwise, False, need to repair.
    """
    # TODO(waihong): Add the check of health when needed.
    return True

  def Repair(self):
    """Repairs the Chameleon.

    It can be an asynchronous call, e.g. do the repair after return. An
    approximate time of the repair is returned. The caller should wait that
    time before the next action.

    Returns:
      An approximate repair time in second.
    """
    # TODO(waihong): Add the repair routine when needed.
    return 0

  def GetSupportedInputs(self):
    """Returns all supported connectors on the board.

    Not like the ProbeInputs() method which only returns the connectors which
    are connected, this method returns all supported connectors on the board.

    Returns:
      A tuple of input_id, for all supported connectors on the board.
    """
    return self._input_flows.keys()

  def IsPhysicalPlugged(self, input_id):
    """Returns if the physical cable is plugged.

    It checks the source power +5V/+3.3V pin.

    Args:
      input_id: The ID of the input connector.

    Returns:
      True if the physical cable is plugged; otherwise, False.
    """
    return self._input_flows[input_id].IsPhysicalPlugged()

  def ProbeInputs(self):
    """Probes all the display connectors on Chameleon board.

    Returns:
      A tuple of input_id, for the connectors connected to DUT.
    """
    input_ids = []
    for input_id in self.GetSupportedInputs():
      if self.IsPhysicalPlugged(input_id):
        input_ids.append(input_id)
    return tuple(input_ids)

  def GetConnectorType(self, input_id):
    """Returns the human readable string for the connector type.

    Args:
      input_id: The ID of the input connector.

    Returns:
      A string, like "VGA", "DVI", "HDMI", or "DP".
    """
    return self._input_flows[input_id].GetConnectorType()

  def WaitVideoInputStable(self, input_id, timeout=None):
    """Waits the video input stable or timeout.

    Args:
      input_id: The ID of the input connector.
      timeout: The time period to wait for.

    Returns:
      True if the video input becomes stable within the timeout period;
      otherwise, False.
    """
    return self._input_flows[input_id].WaitVideoInputStable(timeout)

  def _ReadDefaultEdid(self):
    """Reads the default EDID from file.

    Returns:
      A byte array of EDID data.
    """
    driver_dir = os.path.dirname(os.path.realpath(__file__))
    edid_path = os.path.join(driver_dir, '..', 'data', 'default_edid.bin')
    return open(edid_path).read()

  def CreateEdid(self, edid):
    """Creates an internal record of EDID using the given byte array.

    Args:
      edid: A byte array of EDID data, wrapped in a xmlrpclib.Binary object.

    Returns:
      An edid_id.
    """
    if None in self._all_edids:
      last = self._all_edids.index(None)
      self._all_edids[last] = edid.data
    else:
      last = len(self._all_edids)
      self._all_edids.append(edid.data)
    return last

  def DestroyEdid(self, edid_id):
    """Destroys the internal record of EDID. The internal data will be freed.

    Args:
      edid_id: The ID of the EDID, which was created by CreateEdid().
    """
    if edid_id > 0:
      self._all_edids[edid_id] = None
    else:
      raise DriverError('Not a valid edid_id.')

  def ReadEdid(self, input_id):
    """Reads the EDID content of the selected input on Chameleon.

    Args:
      input_id: The ID of the input connector.

    Returns:
      A byte array of EDID data, wrapped in a xmlrpclib.Binary object.
    """
    return xmlrpclib.Binary(self._input_flows[input_id].ReadEdid())

  def _ApplyDefaultEdid(self):
    """Applies the default EDID to all inputs."""
    logging.info('Apply the default EDID to all inputs.')
    for flow in self._input_flows.itervalues():
      if flow:
        flow.WriteEdid(self._all_edids[0])

  def ApplyEdid(self, input_id, edid_id):
    """Applies the EDID to the selected input.

    Note that this method doesn't pulse the HPD line. Should call Plug(),
    Unplug(), or FireHpdPulse() later.

    Args:
      input_id: The ID of the input connector.
      edid_id: The ID of the EDID.
    """
    self._input_flows[input_id].WriteEdid(self._all_edids[edid_id])

  def IsPlugged(self, input_id):
    """Returns if the HPD line is plugged.

    Args:
      input_id: The ID of the input connector.

    Returns:
      True if the HPD line is plugged; otherwise, False.
    """
    return self._input_flows[input_id].IsPlugged()

  def Plug(self, input_id):
    """Asserts HPD line to high, emulating plug.

    Args:
      input_id: The ID of the input connector.
    """
    return self._input_flows[input_id].Plug()

  def Unplug(self, input_id):
    """Deasserts HPD line to low, emulating unplug.

    Args:
      input_id: The ID of the input connector.
    """
    return self._input_flows[input_id].Unplug()

  def FireHpdPulse(self, input_id, deassert_interval_usec,
                   assert_interval_usec=None, repeat_count=1,
                   end_level=1):
    """Fires one or more HPD pulse (low -> high -> low -> ...).

    Args:
      input_id: The ID of the input connector.
      deassert_interval_usec: The time in microsecond of the deassert pulse.
      assert_interval_usec: The time in microsecond of the assert pulse.
                            If None, then use the same value as
                            deassert_interval_usec.
      repeat_count: The count of HPD pulses to fire.
      end_level: HPD ends with 0 for LOW (unplugged) or 1 for HIGH (plugged).
    """
    if assert_interval_usec is None:
      # Fall back to use the same value as deassertion if not given.
      assert_interval_usec = deassert_interval_usec

    return self._input_flows[input_id].FireHpdPulse(deassert_interval_usec,
        assert_interval_usec, repeat_count, end_level)

  def _SelectInput(self, input_id):
    """Selects the input on Chameleon.

    Args:
      input_id: The ID of the input connector.
    """
    if input_id != self._selected_input:
      self._input_flows[input_id].Select()
      self._selected_input = input_id
    self._input_flows[input_id].Do_FSM()

  def GetPixelFormat(self):
    """Returns the pixel format for the output of DumpPixels.

    Returns:
      A string of the format, like 'rgba', 'bgra', 'rgb', etc.
    """
    return self._PIXEL_FORMAT

  def DumpPixels(self, input_id, x=None, y=None, width=None, height=None):
    """Dumps the raw pixel array of the selected area.

    If not given the area, default to capture the whole screen.

    Args:
      input_id: The ID of the input connector.
      x: The X position of the top-left corner.
      y: The Y position of the top-left corner.
      width: The width of the area.
      height: The height of the area.

    Returns:
      A byte-array of the pixels, wrapped in a xmlrpclib.Binary object.
    """
    self.CaptureVideo(input_id, self._DEFAULT_FRAME_LIMIT, x, y, width, height)
    return self.ReadCapturedFrame(self._DEFAULT_FRAME_INDEX)

  def GetMaxFrameLimit(self, input_id, width, height):
    """Gets the maximal number of frames which are accommodated in the buffer.

    It depends on the size of the internal buffer on the board and the
    size of area to capture (full screen or cropped area).

    Args:
      input_id: The ID of the input connector.
      width: The width of the area to capture.
      height: The height of the area to capture.

    Returns:
      A number of the frame limit.
    """
    return self._input_flows[input_id].GetMaxFrameLimit(width, height)

  def CaptureVideo(self, input_id, total_frame, x=None, y=None, width=None,
                   height=None):
    """Captures the video stream on the given input to the buffer.

    This API is a synchronous call. It returns after all the frames are
    captured. The frames can be read using the ReadCapturedFrame API.

    The example of usage:
      chameleon.CaptureVideo(hdmi_input, total_frame)
      for i in xrange(total_frame):
        frame = chameleon.ReadCapturedFrame(i, *area).data
        CompareFrame(frame, golden_frames[i])

    Args:
      input_id: The ID of the input connector.
      total_frame: The total number of frames to capture, should not larger
                   than value of GetMaxFrameLimit.
      x: The X position of the top-left corner of crop.
      y: The Y position of the top-left corner of crop.
      width: The width of the area of crop.
      height: The height of the area of crop.

    Returns:
      A byte-array of the pixels, wrapped in a xmlrpclib.Binary object.
    """
    self._SelectInput(input_id)
    if not self.IsPlugged(input_id):
      raise DriverError('HPD is unplugged. No signal is expected.')

    max_frame_limit = self.GetMaxFrameLimit(input_id, width, height)
    if total_frame > max_frame_limit:
      raise DriverError('Exceed the max frame limit %d > %d',
                        total_frame, max_frame_limit)

    is_dual_pixel_mode = self._input_flows[input_id].IsDualPixelMode()
    if None not in (x, y, width, height):
      if is_dual_pixel_mode:
        if any((x % 16, y % 8, width % 16, height % 8)):
          raise DriverError('Argument not aligned')
      else:
        if any((x % 8, y % 8, width % 8, height % 8)):
          raise DriverError('Argument not aligned')

    # TODO(waihong): Make the timeout value based on the FPS rate.
    self._input_flows[input_id].DumpFramesToLimit(
        total_frame, x, y, width, height, self._TIMEOUT_FRAME_DUMP_PROBE)

    if None in (width, height):
      width, height = self._input_flows[input_id].GetResolution()

    self._captured_params = {
      'total_frame': total_frame,
      'input_id': input_id,
      'resolution': (width, height),
      'is_dual_pixel': is_dual_pixel_mode,
      'pixeldump_args': self._input_flows[input_id].GetPixelDumpArgs(),
    }

  def GetCapturedResolution(self):
    """Gets the resolution of the captured frame.

    If a cropping area is specified on capturing, returns the cropped
    resolution.

    Returns:
      A (width, height) tuple.
    """
    return self._captured_params['resolution']

  def ReadCapturedFrame(self, frame_index):
    """Reads the content of the captured frames from the buffer.

    Args:
      frame_index: The index of the frame to read.

    Returns:
      A byte-array of the pixels, wrapped in a xmlrpclib.Binary object.
    """
    total_frame = self._captured_params['total_frame']
    if not 0 <= frame_index < total_frame:
      raise DriverError('The frame index is out-of-range: %d not in [0, %d)' %
                        (frame_index, total_frame))
    width, height = self.GetCapturedResolution()
    # Specify the proper arguemnt for dual-buffer capture.
    if self._captured_params['is_dual_pixel']:
      width = width / 2

    # Modify the memory offset to match the frame.
    PAGE_SIZE = 4096
    frame_size = width * height * len(self._PIXEL_FORMAT)
    frame_size = ((frame_size - 1) / PAGE_SIZE + 1) * PAGE_SIZE
    offset = frame_size * frame_index
    offset_args = []
    for arg in self._captured_params['pixeldump_args']:
      if isinstance(arg, (int, long)):
        offset_args.append(arg + offset)
      else:
        offset_args.append(arg)
    logging.info('pixeldump args %r', offset_args)

    with tempfile.NamedTemporaryFile() as f:
      self._tools.Call('pixeldump', f.name, width, height,
                       len(self._PIXEL_FORMAT), *offset_args)
      screen = f.read()
    return xmlrpclib.Binary(screen)

  def GetCapturedChecksums(self, start_index, stop_index):
    """Gets the list of checksums of the captured frames.

    Args:
      start_index: The index of the start frame.
      stop_index: The index of the stop frame (excluded).

    Returns:
      The list of checksums of frames.
    """
    input_id = self._captured_params['input_id']
    return self._input_flows[input_id].GetFrameHashes(start_index, stop_index)

  def ComputePixelChecksum(self, input_id, x=None, y=None, width=None,
        height=None):
    """Computes the checksum of pixels in the selected area.

    If not given the area, default to compute the whole screen.

    Args:
      input_id: The ID of the input connector.
      x: The X position of the top-left corner.
      y: The Y position of the top-left corner.
      width: The width of the area.
      height: The height of the area.

    Returns:
      The checksum of the pixels.
    """
    self.CaptureVideo(input_id, self._DEFAULT_FRAME_LIMIT, x, y, width, height)
    return self.GetCapturedChecksums(self._DEFAULT_FRAME_INDEX,
                                     self._DEFAULT_FRAME_INDEX + 1)[0]

  def DetectResolution(self, input_id):
    """Detects the source resolution.

    Args:
      input_id: The ID of the input connector.

    Returns:
      A (width, height) tuple.
    """
    self._SelectInput(input_id)
    return self._input_flows[input_id].GetResolution()

  def _CheckInputIdSupportAudio(self, input_id):
    """Checks if the input has audio support.

    Args:
      input_id: The ID of the input connector.
    """
    if input_id not in self._INPUTS_AUDIO_SUPPORTED:
      raise DriverError('Not a valid input_id for audio operation.')

  def StartCapturingAudio(self, input_id):
    """Starts capturing audio.

    Args:
      input_id: The ID of the input connector.
    """
    self._CheckInputIdSupportAudio(input_id)
    self._SelectInput(input_id)
    self._input_flows[input_id].StartCapturingAudio()

  def StopCapturingAudio(self, input_id):
    """Stops capturing audio and returns recorded audio raw data.

    Args:
      input_id: The ID of the input connector.

    Returns:
      A tuple (data, format).
      data: The captured audio data wrapped in an xmlrpclib.Binary object.
      format: The dict representation of AudioDataFormat. Refer to docstring
        of utils.audio.AudioDataFormat for detail.
    """
    self._CheckInputIdSupportAudio(input_id)
    data, data_format = self._input_flows[input_id].StopCapturingAudio()
    return xmlrpclib.Binary(data), data_format