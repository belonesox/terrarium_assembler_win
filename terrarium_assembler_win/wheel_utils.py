"""
Parse wheel filenames

``wheel-filename`` lets you verify `wheel
<https://www.python.org/dev/peps/pep-0427/>`_ filenames and parse them into
their component fields.

This package adheres strictly to the relevant PEPs, with the following
exceptions:

- Unlike other filename components, version components may contain the
  characters ``!`` and ``+`` for full PEP 440 support.

- Version components may be any sequence of the relevant set of characters;
  they are not verified for PEP 440 compliance.

- The ``.whl`` file extension is matched case-insensitively.

Visit <https://github.com/jwodder/wheel-filename> for more information.
"""

__all__ = [
    'InvalidFilenameError',
    'ParsedWheelFilename',
    'parse_wheel_filename',
]

import os
import os.path
import re
from   typing import Iterable, List, NamedTuple, Optional, Union

# These patterns are interpreted with re.UNICODE in effect, so there's probably
# some character that matches \d but not \w that needs to be included
PYTHON_TAG_RGX   = r'[\w\d]+'
ABI_TAG_RGX      = r'[\w\d]+'
PLATFORM_TAG_RGX = r'[\w\d]+'

WHEEL_FILENAME_CRGX = re.compile(
    r'(?P<project>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)'
    r'-(?P<version>[A-Za-z0-9_.!+]+)'
    r'(?:-(?P<build>[0-9][\w\d.]*))?'
    r'(?:-(?P<python_tags>{0}(?:\.{0})*))?'
    r'(?:-(?P<abi_tags>{1}(?:\.{1})*))?'
    r'(?:-(?P<platform_tags>{2}(?:\.{2})*))?'
    r'\.([Ww][Hh][Ll]|tar\.gz|tar\.bz2)'
    .format(PYTHON_TAG_RGX, ABI_TAG_RGX, PLATFORM_TAG_RGX)
)

class ParsedWheelFilename(NamedTuple):
    project: str
    version: str
    build: Optional[str]
    python_tags: List[str]
    abi_tags: List[str]
    platform_tags: List[str]

    # def __str__(self) -> str:
    #     if self.build:
    #         fmt = '{0.project}-{0.version}-{0.build}-{1}-{2}-{3}.whl'
    #     else:
    #         fmt = '{0.project}-{0.version}-{1}-{2}-{3}.whl'
    #     return fmt.format(
    #         self,
    #         '.'.join(self.python_tags),
    #         '.'.join(self.abi_tags),
    #         '.'.join(self.platform_tags),
    #     )

    def tag_triples(self) -> Iterable[str]:
        """
        Returns a generator of all simple tag triples formed from the tags in
        the filename
        """
        for py in self.python_tags:
            for abi in self.abi_tags:
                for plat in self.platform_tags:
                    yield '-'.join([py, abi, plat])


def parse_wheel_filename(
    filename: Union[str, bytes, "os.PathLike[str]", "os.PathLike[bytes]"]
) -> ParsedWheelFilename:
    """
    Parse a wheel filename into its components

    :param path filename: a wheel path or filename
    :rtype: ParsedWheelFilename
    :raises InvalidFilenameError: if the filename is invalid
    """
    basename = os.path.basename(os.fsdecode(filename))
    m = WHEEL_FILENAME_CRGX.fullmatch(basename)
    if not m:
        raise InvalidFilenameError(basename)

    python_tags_   = m.group('python_tags')
    if python_tags_:
        python_tags_ = python_tags_.split()
    abi_tags_   = m.group('abi_tags')
    if abi_tags_:    
        abi_tags_      = abi_tags_.split()
    platform_tags_   = m.group('platform_tags')
    if platform_tags_:    
        platform_tags_ = platform_tags_.split()

    return ParsedWheelFilename(
        project       = m.group('project'),
        version       = m.group('version'),
        build         = m.group('build'),
        python_tags   = python_tags_,
        abi_tags      = abi_tags_,
        platform_tags = platform_tags_,
    )


class InvalidFilenameError(ValueError):
    """ Raised when an invalid wheel filename is encountered """

    filename: str

    def __init__(self, filename: str) -> None:
        #: The invalid filename
        self.filename = filename

    def __str__(self) -> str:
        return 'Invalid wheel filename: ' + repr(self.filename)
