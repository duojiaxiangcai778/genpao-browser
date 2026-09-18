# UTF-8
#
# For more details about fixed file info 'ffi' see:
# https://learn.microsoft.com/en-us/windows/win32/menurc/versioninfo-resource
#
version_info = VSVersionInfo(
  ffi=FixedFileInfo(
    # filevers and prodvers should be always a tuple of four items: (1, 2, 3, 4)
    filevers=(1, 0, 6, 0),
    prodvers=(1, 0, 6, 0),
    # Contains a bitwise combination of the file's flags
    mask=0x3f,
    flags=0x0,
    # OS type. 0x40004 = NT
    OS=0x40004,
    # File type. 0x1 = EXE
    fileType=0x1,
    # Nothing is registered
    subtype=0x0,
    # Date and time stamps
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [StringStruct(u'CompanyName', u''),
          StringStruct(u'FileDescription', u'跟跑助手'),
          StringStruct(u'FileVersion', u'1.0.6.0'),
          StringStruct(u'InternalName', u'跟跑助手'),
          StringStruct(u'LegalCopyright', u''),
          StringStruct(u'OriginalFilename', u'跟跑助手.exe'),
          StringStruct(u'ProductName', u'跟跑浏览器'),
          StringStruct(u'ProductVersion', u'1.0.6')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [0x0409, 0x04B0])])
  ]
)