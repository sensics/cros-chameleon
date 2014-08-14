# Copyright (c) 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.


"""Provides Extension class and related classes with methods for parsing info.

Extensions are found after the 128-byte base EDID.
The various types of Extensions all inherit the basic Extension object.
"""

import coordinated_video_timings as cvt_module
import data_block
import descriptor
import error
import standard_timings


TYPE_TIMING_EXTENSION = 'Timing Extension'
TYPE_CEA_861 = 'CEA-861 Series Timing Extension'
TYPE_VIDEO_TIMING_BLOCK = 'Video Timing Block Extension (VTB-EXT)'
TYPE_DISPLAY_INFORMATION = 'Display Information Extension (DI-EXT)'
TYPE_LOCALIZED_STRING = 'Localized String Extension (LS-EXT)'
TYPE_DIGITAL_PACKET_VIDEO_LINK = ('Digital Packet Video Link Extension '
                                  '(DPVL-EXT)')
TYPE_EXTENSION_BLOCK_MAP = 'Extension Block Map'
TYPE_MANUFACTURER_EXTENSION = 'Extension defined by monitor manufacturer'
TYPE_UNKNOWN = 'Unknown Extension type'


def GetExtension(edid, index, version):
  """Fetches an extension to an EDID.

  Args:
    edid: The list form of the EDID being analyzed.
    index: An integer indicating the index of the extension.
    version: The EDID version (usually 1.3 or 1.4).

  Returns:
    An Extension object.
  """
  block = edid[128 * index : 128 * (index + 1)]

  tag = block[0]

  if tag == 0x00:
    return TimingExtension(block)
  elif tag == 0x02:
    return CEAExtension(block, version)
  elif tag == 0x10:
    return VTBExtension(block, version)
  elif tag == 0x40:
    return DisplayInformationExtension(block)
  elif tag == 0x50:
    return LocalizedStringExtension(block)
  elif tag == 0x60:
    return DPVLExtension(block)
  elif tag == 0xF0:
    return ExtensionBlockMap(block)
  elif tag == 0xFF:
    return ManufacturerExtension(block)
  else:
    return Extension(block, TYPE_UNKNOWN)


class Extension(object):
  """Defines a basic extension."""

  def __init__(self, block, my_type, version=None):
    """Creates an Extension object.

    Args:
      block: A list of bytes that make up the extension.
      my_type: A string indicating the type of extension.
      version: The EDID version.
    """
    self._block = block
    self._type = my_type
    self._version = version

  @property
  def tag(self):
    """Fetches the tag of the extension.

    Returns:
      An integer indicating the tag of the extension.
    """
    return self._block[0]

  @property
  def type(self):
    """Fetches the type of the extension.

    Returns:
      A string indicating the type of the extension.
    """
    return self._type

  def GetBlock(self):
    """Fetches the bytes that make up the extension.

    Returns:
      A list of bytes that make up the extension.
    """
    return self._block

  def CheckErrors(self, index=None):
    """Creates a method for error checking to define in other Extensions.

    Args:
      index: The integer index of the extension being checked.
    """
    pass


class TimingExtension(Extension):
  """Defines a Timing Extension."""

  def __init__(self, block):
    """Creates a TimingExtension object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_TIMING_EXTENSION)


class CEAExtension(Extension):
  """Defines a CEA Extension, perhaps the most common type."""

  def __init__(self, block, version):
    """Creates a CEAExtension object.

    Args:
      block: The list of bytes that make up the extension.
      version: The version of the extension.
    """
    Extension.__init__(self, block, TYPE_CEA_861, version)
    self._dtd_start = block[2]

  @property
  def dtd_offset(self):
    """Fetches the start index of the Detailed Timing Descriptors.

    Returns:
      An integer indicating the start index of Detailed Timing Descriptors.
    """
    return self._dtd_start

  @property
  def version(self):
    """Fetches the extension version.

    Returns:
      An integer indicating the extension version.
    """
    return self._block[1]

  @property
  def underscan_support(self):
    """Fetches whether underscan is supported.

    Returns:
      A boolean indicating whether underscan is supported.
    """
    return bool(self._block[3] & 0x80)

  @property
  def basic_audio_support(self):
    """Fetches whether basic audio is supported.

    Returns:
      A boolean indicating whether basic audio is supported.
    """
    return bool(self._block[3] & 0x40)

  @property
  def ycbcr444_support(self):
    """Fetches whether YCbCr 4:4:4 is supported.

    Returns:
      A boolean indicating whether YCbCr 4:4:4 is supported.
    """
    return bool(self._block[3] & 0x20)

  @property
  def ycbcr422_support(self):
    """Fetches whether YCbCr 4:2:2 is supported.

    Returns:
      A boolean indicating whether YCbCr 4:2:2 is supported.
    """
    return bool(self._block[3] & 0x10)

  @property
  def native_dtd_count(self):
    """Fetches the number of native Detailed Timing Descriptors.

    Returns:
      An integer indicating the number of native Detailed Timing Descriptors.
    """
    return self._block[3] & 0x0F

  @property
  def data_blocks(self):
    """Fetches the Data Block objects.

    Returns:
      A list of Data Block objects.
    """
    # DTDs begin immediately, meaning there are no data blocks
    if self._dtd_start == 4:
      return None

    dbs = []

    current = 4

    while current < self._dtd_start:
      db = data_block.GetDataBlock(self._block, current)
      dbs.append(db)

      current += db.length + 1

    return dbs

  @property
  def dtds(self):
    """Fetches the descriptor.DetailedTimingDescriptor objects.

    Returns:
      A list of descriptor.DetailedTimingDescriptor objects.
    """
    dtds = []

    for x in xrange(self._dtd_start, self._GetPadIndex(), 18):
      dtd = descriptor.GetDescriptor(self._block, x, self._version)
      dtds.append(dtd)

    return dtds

  def _GetPadIndex(self):
    """Fetches the start index of post-DTD padding.

    Returns:
      An integer indicating the start index of post-DTD padding.
    """
    for x in xrange(self._dtd_start, 127, 18):
      if self._block[x] == self._block[x + 1] == 0:
        return x
    return 127 - (127 - self._dtd_start) % 18

  def CheckErrors(self, index=None):
    """Checks the extension for errors.

    All bytes after the end of DTDs should be 0x00.

    Args:
      index: The integer index of the extension.

    Returns:
      An Error object.
    """
    ext_index = '(Extension #%d)' % index if index else ''
    padding_start = self._GetPadIndex()
    padding = self._block[padding_start : 127]

    if padding != [0] * len(padding):
      found = '%02X ' * len(padding) % tuple(padding,)
      return [error.Error('CEA extension %s' % ext_index, 'All bytes after DTDs'
                          ' should be 0x00', 'All 0x00s', found)]
    else:
      return None


class VTBExtension(Extension):
  """Defines a VTB Extension."""

  def __init__(self, block, version):
    """Creates a VTBExtension object.

    Args:
      block: The list of bytes that make up the extension.
      version: The version of the extension.
    """
    Extension.__init__(self, block, TYPE_VIDEO_TIMING_BLOCK, version)
    self._dtb_count = block[2]
    self._cvt_count = block[3]
    self._st_count = block[4]

  @property
  def version(self):
    """Fetches the VTB extension version.

    Returns:
      An integer indicating the version.
    """
    return self._block[1]

  @property
  def dtb_count(self):
    """Fetches the number of DetailedTimingDescriptor objects.

    Returns:
      An integer indicating the number of DetailedTimingDescriptor objects.
    """
    return self._dtb_count

  @property
  def cvt_count(self):
    """Fetches the number of CoordinatedVideoTiming objects.

    Returns:
      An integer indicating the number of CoordinatedVideoTiming objects.
    """
    return self._cvt_count

  @property
  def st_count(self):
    """Fetches the number of StandardTiming objects.

    Returns:
      An integer indicating the number of StandardTiming objects.
    """
    return self._st_count

  @property
  def dtbs(self):
    """Fetches the descriptor.DetailedTimingDescriptors.

    Returns:
      A list of descriptor.DetailedTimingDescriptors.
    """
    dtbs = []
    dtb_start = 5

    for x in xrange(0, self._dtb_count):
      dtd = descriptor.GetDescriptor(self._block, dtb_start + (x * 18),
                                     self._version)
      dtbs.append(dtd)

    return dtbs

  @property
  def cvts(self):
    """Fetches the coordinated_video_timings.CoordinatedVideoTiming objects.

    Returns:
      A list of coordinated_video_timings.CoordinatedVideoTiming objects.
    """
    cvts = []
    cvt_start = 5 + (18 * self._dtb_count)

    for x in xrange(0, self._cvt_count):
      cvt = cvt_module.GetCoordinatedVideoTiming(self._block,
                                                 cvt_start + (x * 3))
      cvts.append(cvt)

    return cvts

  @property
  def sts(self):
    """Fetches the standard_timings.StandardTiming objects.

    Returns:
      A list of standard_timings.StandardTiming objects.
    """
    sts = []
    st_start = 5 + (18 * self._dtb_count) + (3 * self._cvt_count)

    for x in xrange(0, self._st_count):
      st = standard_timings.GetStandardTiming(self._block, st_start + (x * 2),
                                              self._version)
      sts.append(st)

    return sts

  def CheckErrors(self, index=None):
    """Checks the extension for errors.

    Args:
      index: The integer index of the extension.

    Returns:
      A list of error.Error objects.
    """
    errors = []

    cvts = self.cvts
    for x in xrange(0, self._cvt_count):
      err = cvts[x].CheckErrors(x + 1)
      if err:
        errors.extend(err)

    sts = self.sts
    for x in xrange(0, self._st_count):
      err = sts[x].CheckErrors(x + 1)
      if err:
        errors.extend(err)

    # Check that all bytes after standard timing blocks end are 0x00
    unused_start = (5 + (18 * self._dtb_count) + (3 * self._cvt_count) +
                    (2 * self._st_count))

    ext_index = '(Extension #%d)' % index if index else ''
    padding = self._block[unused_start : 127]
    if padding != [0] * len(padding):
      found = '0x%02X ' * len(padding) % tuple(padding,)
      errors.append(error.Error('VTB Extension %s' % ext_index,
                                'All bytes after STs should be 0x00',
                                'All 0x00s', found))
    return errors


class DisplayInformationExtension(Extension):
  """Analyzes a Display Information Extension."""

  def __init__(self, block):
    """Creates a DisplayInformationExtension object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_DISPLAY_INFORMATION)


class LocalizedStringExtension(Extension):
  """Analyzes a Localized String Extension."""

  def __init__(self, block):
    """Creates a LocalizedStringExtension object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_LOCALIZED_STRING)


class DPVLExtension(Extension):
  """Analyzes a Digital Packet Video Link Extension."""

  def __init__(self, block):
    """Creates a DPVLExtension object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_DIGITAL_PACKET_VIDEO_LINK)


class ExtensionBlockMap(Extension):
  """Defines an Extension Block Map (which is also an extension)."""

  def __init__(self, block):
    """Creates an ExtensionBlockMap object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_EXTENSION_BLOCK_MAP)

  @property
  def all_tags(self):
    """Fetches all 126 tags in the block map.

    Zeroes indicate unused blocks.

    Returns:
      A list of 126 tags (integers) indicating the tag of each following block.
    """
    return self._block[1:127]


class ManufacturerExtension(Extension):
  """Defines a Manufacturer Extension."""

  def __init__(self, block):
    """Creates a ManufacturerExtension object.

    Args:
      block: The list of bytes that make up the extension.
    """
    Extension.__init__(self, block, TYPE_MANUFACTURER_EXTENSION)

