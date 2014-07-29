# Copyright (c) 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""FPGA module for controlling the functions of FPGA.

Using the FpgaController can control all its subsystems.
Usage:
  import fpga
  fpga_ctrl = fpga.FpgaController()

  # Control the HPD
  fpga_ctrl.hpd.Plug(input_id)
  fpga_ctrl.hpd.Unplug(input_id)

  # Select the video pass-through.
  fpga_ctrl.vpass.Select(input_id)
"""

import struct

import chameleon_common  # pylint: disable=W0611
from chameleond.utils import ids
from chameleond.utils import mem_native as mem


class FpgaController(object):
  """A class to abstract the all behaviors of FPGA.

  An instance of this class also includes the instances of its subsystems.
  The caller can directly access them via the member variables.
  """

  def __init__(self):
    """Constructs a FpgaController object."""
    self.hpd = HpdController()
    self.vpass = VideoPasser()
    self.vdump0 = VideoDumper(0)
    self.vdump1 = VideoDumper(1)
    self.hdmi_edid = EdidController(EdidController.HDMI_BASE)
    self.vga_edid = EdidController(EdidController.VGA_BASE)


class HpdController(object):
  """A class to abstract the behavior of HPD."""

  _HPD_BASE = 0xff21a000
  _HPD_OFFSETS = {
    ids.DP1: 0x4,
    ids.DP2: 0x8,
    ids.HDMI: 0xc
  }
  _BIT_UNPLUG = 0
  _BIT_PLUG = 1

  def __init__(self):
    """Constructs a HpdController object."""
    self._memory = mem.Memory

  def IsPlugged(self, input_id):
    """Returns if the HPD line is plugged.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.

    Returns:
      True if the HPD line is plugged; otherwise, False.
    """
    return (self._memory.Read(self._HPD_BASE + self._HPD_OFFSETS[input_id]) ==
            self._BIT_PLUG)

  def Plug(self, input_id):
    """Asserts HPD line to high, emulating plug.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
    """
    self._memory.Write(self._HPD_BASE + self._HPD_OFFSETS[input_id],
                       self._BIT_PLUG)

  def Unplug(self, input_id):
    """Deasserts HPD line to low, emulating unplug.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
    """
    self._memory.Write(self._HPD_BASE + self._HPD_OFFSETS[input_id],
                       self._BIT_UNPLUG)


class VideoPasser(object):
  """A class to abstract the behavior of video pass-through.

  The pass-through video is output to the VGA output on the main board.
  """

  _REG_CTRL = 0xff21d004

  _BIT_DATA_A = 0
  _BIT_DATA_B = 1 << 0
  _BIT_CLK_A = 0
  _BIT_CLK_B = 1 << 1

  _VALUES_CTRL = {
    ids.DP1: _BIT_CLK_A | _BIT_DATA_A,
    ids.DP2: _BIT_CLK_B | _BIT_DATA_B,
    ids.HDMI: _BIT_CLK_B | _BIT_DATA_A,
    ids.VGA: _BIT_CLK_A | _BIT_DATA_A
  }

  def __init__(self):
    """Constructs a VideoPasser object."""
    self._memory = mem.Memory

  def Select(self, input_id):
    """Selects the given input for pass-through.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
    """
    self._memory.Write(self._REG_CTRL, self._VALUES_CTRL[input_id])


class VideoDumper(object):
  """A class to control video dumper."""

  _REGS_BASE = (0xff210000,  # Dumper 0
                0xff211000)  # Dumper 1
  # Control register
  _REG_CTRL = 0x0
  _BIT_CLK_NORMAL = 0
  _BIT_CLK_ALT = 1 << 1
  _BIT_STOP = 0
  _BIT_RUN = 1 << 2
  # Run only when both dumpers' _BIT_RUN_DUAL set.
  _BIT_RUN_DUAL = 1 << 3
  # Set to generate 64-bit frame hash; otherwise, 32-bit.
  _BIT_HASH_64 = 1 << 4
  # Set to enable cropping.
  _BIT_CROP = 1 << 5

  # Register which stores the offsets, related to 0xc0000000, for dump.
  _REG_START_ADDR = 0x8
  _REG_END_ADDR = 0xc
  _REG_LOOP = 0x10
  _REG_LIMIT = 0x14
  # Registers to get the width and height
  _REG_WIDTH = 0x18
  _REG_HEIGHT = 0x1c
  _REG_FRAME_COUNT = 0x20
  # Registers to crop frames
  _REG_CROP_XRANGE = 0x24
  _REG_CROP_YRANGE = 0x28

  # Frame hash buffer
  _REG_HASH_BUF_BASE = 0x400
  _REG_HASH_BUF_SIZE = 1024

  #  Input                           | DP1 | DP2 | HDMI | CRT |
  # -----------------------------------------------------------
  #  (1) CLOCK                       | A   | B   | B    | A   |
  # -----------------------------------------------------------
  #  (2) SINGLE PIXEL DATA           | A   | B   | B    | A   |
  #  (3) DUAL PIXEL EVEN PIXELS DATA | A   | B   | A    |     |
  #  (4) DUAL PIXEL ODD PIXELS DATA  | B   | A   | B    |     |
  PRIMARY_FLOW_INDEXES = {
    ids.DP1: 0,
    ids.DP2: 1,
    ids.HDMI: 1,
    ids.VGA: 0,
  }
  EVEN_PIXELS_FLOW_INDEXES = {
    ids.DP1: 0,
    ids.DP2: 1,
    ids.HDMI: 0,
    ids.VGA: 0,
  }

  _DUMP_BASE_ADDRESS = 0xc0000000
  _DUMP_BUFFER_SIZE = 0x1c000000
  _DUMP_START_ADDRESSES = (0x00000000,  # Dumper 0
                           0x20000000)  # Dumper 1
  _DEFAULT_LIMIT = 1
  _DEFAULT_ENABLE_LOOP = False

  def __init__(self, index):
    """Constructs a VideoDumper object.

    Args:
      index: 0 for Dumper A and 1 for Dumper B.
    """
    self._memory = mem.Memory
    self._index = index

  def EnableCrop(self, x, y, width, height):
    """Enable cropping frames.

    Only dump the pixels and its checksum within the given rectangle.

    Args:
      x: The left column index of the rectangle.
      y: The top row index of the rectangle.
      width: The width of the rectangle.
      height: The height of the rectangle.
    """
    right = x + width
    bottom = y + height
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_CROP_XRANGE,
                       right << 16 | x)
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_CROP_YRANGE,
                       bottom << 16 | y)
    self._memory.SetMask(self._REGS_BASE[self._index] + self._REG_CTRL,
                         self._BIT_CROP)

  def DisableCrop(self):
    """Disable cropping frames."""
    self._memory.ClearMask(self._REGS_BASE[self._index] + self._REG_CTRL,
                           self._BIT_CROP)

  def Stop(self):
    """Stops dumping."""
    self._memory.ClearMask(self._REGS_BASE[self._index] + self._REG_CTRL,
                           self._BIT_RUN | self._BIT_RUN_DUAL)

  def Start(self, input_id, dual_pixel_mode):
    """Starts dumping.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
      dual_pixel_mode: True to use dual pixel mode; otherwise, False.
    """
    if dual_pixel_mode:
      bit_run = self._BIT_RUN_DUAL
    elif self._index == self.PRIMARY_FLOW_INDEXES[input_id]:
      bit_run = self._BIT_RUN
    else:
      return
    self._memory.SetMask(self._REGS_BASE[self._index] + self._REG_CTRL, bit_run)

  @classmethod
  def GetMaxFrameLimit(cls, width, height):
    """Returns of the maximal number of frames which can be dumped."""
    BYTE_PER_PIXEL = 3
    PAGE_SIZE = 4096
    frame_size = width * height * BYTE_PER_PIXEL
    frame_size = ((frame_size - 1) / PAGE_SIZE + 1) * PAGE_SIZE
    return cls._DUMP_BUFFER_SIZE / frame_size

  def SetFrameLimit(self, frame_limit, loop=False):
    """Sets the limitation of total frames to dump.

    Args:
      frame_limit: The number of frames to dump.
      loop: When the frame_limit is reached, True to reset the dump pointer
            to the start address; False to do nothing.
    """
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_LIMIT,
                       frame_limit)
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_LOOP,
                       1 if loop else 0)

  def Select(self, input_id, dual_pixel_mode):
    """Selects the given input for dumping.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
      dual_pixel_mode: True to use dual pixel mode; otherwise, False.
    """
    self.Stop()
    # Set the memory addresses, loop, and limit for dump.
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_START_ADDR,
                       self._DUMP_START_ADDRESSES[self._index])
    self._memory.Write(self._REGS_BASE[self._index] + self._REG_END_ADDR,
                       self._DUMP_START_ADDRESSES[self._index] +
                         self._DUMP_BUFFER_SIZE)
    self.SetFrameLimit(self._DEFAULT_LIMIT, self._DEFAULT_ENABLE_LOOP)
    # Use the proper CLK.
    if self._index == self.PRIMARY_FLOW_INDEXES[input_id]:
      ctrl_value = self._BIT_CLK_NORMAL
    else:
      ctrl_value = self._BIT_CLK_ALT
    # Use the proper hash mode.
    if not dual_pixel_mode:
      ctrl_value = ctrl_value | self._BIT_HASH_64

    self._memory.Write(self._REGS_BASE[self._index] + self._REG_CTRL,
                       ctrl_value)
    # Start dumping.
    self.Start(input_id, dual_pixel_mode)

  def GetWidth(self):
    """Gets the width of the video path."""
    return self._memory.Read(self._REGS_BASE[self._index] + self._REG_WIDTH)

  def GetHeight(self):
    """Gets the height of the video path."""
    return self._memory.Read(self._REGS_BASE[self._index] + self._REG_HEIGHT)

  def GetFrameCount(self):
    """Gets the total count of frames captured."""
    return self._memory.Read(self._REGS_BASE[self._index] +
                             self._REG_FRAME_COUNT)

  def GetFrameHash(self, index, dual_pixel_mode):
    """Gets the frame hash of the given frame index.

    FPGA overwrites the old hash values when exceeding the hash buffer
    size. The caller should save the old values before that happens.

    Args:
      index: The index of frame. The index can exceed the hash buffer size.
      dual_pixel_mode: True if using the dual pixel mode; otherwise, False.

    Returns:
      A list of hash16 values.
    """
    hash_addr = lambda x: (self._REGS_BASE[self._index] +
        self._REG_HASH_BUF_BASE + (x * 4) % self._REG_HASH_BUF_SIZE)

    if dual_pixel_mode:
      hash32 = self._memory.Read(hash_addr(index))
      return [hash32 >> 16, hash32 & 0xffff]
    else:
      hash32s = [self._memory.Read(hash_addr(index * 2 + i)) for i in (0, 1)]
      return [hash32s[1] >> 16, hash32s[1] & 0xffff,
              hash32s[0] >> 16, hash32s[0] & 0xffff]

  @classmethod
  def GetPixelDumpArgs(cls, input_id, dual_pixel_mode):
    """Gets the arguments of pixeldump tool which selects the proper buffers.

    Args:
      input_id: The ID of the input connector. Check the value in ids.py.
      dual_pixel_mode: True to use dual pixel mode; otherwise, False.
    """
    i = cls.PRIMARY_FLOW_INDEXES[input_id]
    if dual_pixel_mode:
      i = cls.EVEN_PIXELS_FLOW_INDEXES[input_id]
      return ['-a', cls._DUMP_BASE_ADDRESS + cls._DUMP_START_ADDRESSES[i],
              '-b', cls._DUMP_BASE_ADDRESS + cls._DUMP_START_ADDRESSES[1 - i]]
    else:
      return ['-a', cls._DUMP_BASE_ADDRESS + cls._DUMP_START_ADDRESSES[i]]


class EdidController(object):
  """A class to abstract the behavior of the EDID controller."""

  HDMI_BASE = 0xff217000
  VGA_BASE = 0xff219000
  _REG_CTRL = 0
  _BIT_RESET = 0
  _BIT_OPERATE = 1
  _EDID_MEM = 0x100

  _EDID_SIZE = 256

  def __init__(self, edid_base):
    """Constructs a EdidController object.

    Args:
      edid_base: The base of the memory address which stores the EDID.
    """
    self._memory = mem.Memory
    self._edid_base = edid_base

  def WriteEdid(self, data):
    """Writes the EDID content.

    Args:
      data: The EDID control to write.
    """
    for offset in range(0, len(data), 4):
      value = struct.unpack('>I', data[offset:offset+4])[0]
      self._memory.Write(self._edid_base + self._EDID_MEM + offset, value)
    self._memory.Write(self._edid_base + self._REG_CTRL, self._BIT_OPERATE)

  def ReadEdid(self):
    """Reads the EDID content.

    Returns:
      A byte array of EDID data.
    """
    all_value = ''
    for offset in range(0, self._EDID_SIZE, 4):
      value = self._memory.Read(self._edid_base + self._EDID_MEM + offset)
      all_value += struct.pack('>I', value)
    return all_value
