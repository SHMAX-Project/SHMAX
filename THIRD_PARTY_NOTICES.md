# Third-party notices / Сторонние компоненты

SHMAX source is AGPL-3.0-or-later. Third-party components retain their own
licenses and copyrights; the SHMAX license does not replace them.

The v0.1.0 inventory is based on the committed Python requirements.lock and
npm package-lock.json and the installed distributions used for validation:

- [Python versions, license expressions and license files](release/v0.1.0/DEPENDENCY_LICENSES.json).
- [Node versions and license expressions, including build/test dependencies](release/v0.1.0/NODE_LICENSES.json).
- [Preserved upstream copyright/license texts](third-party-licenses/).

All 67 pinned Python distributions have installed license files. All 323 npm
lock entries have license metadata; optional platform-specific packages need
not be installed on this Mac. The copied notices include the installed packages.
Runtime frontend imports React, React DOM, React Router, i18next and react-i18next;
the production archive includes their compiled code and preserved license texts.
Backend dependencies are installed from the lock rather than bundled as wheels
inside the source/production tarballs. Preserve installed distribution notices
when distributing container images. This inventory is a release review, not a
claim that metadata replaces legal obligations or a security vulnerability audit.

Important corrections and obligations:

- FastAPI is MIT, not BSD: [upstream license](https://github.com/fastapi/fastapi/blob/master/LICENSE).
- PyMax/maxapi-python 2.4.1: MIT, retained in the Python notices.
- certifi: MPL-2.0. Preserve notices and availability of corresponding covered source.
- Pillow: MIT-CMU; greenlet includes MIT and PSF-2.0; aiohttp includes Apache-2.0
  and MIT; cryptography offers Apache-2.0 or BSD-3-Clause.
- Xray-core v25.3.6 is separately fetched with pinned SHA256 during Docker build.
  Its binary is not in these tarballs but is included in built Docker images.
  [Covered source at the pinned tag](https://github.com/XTLS/Xray-core/tree/v25.3.6)
  and [MPL-2.0 license](https://github.com/XTLS/Xray-core/blob/v25.3.6/LICENSE)
  must remain available when distributing that image. No image is published here.
- System fonts are used; no separate font files are distributed.
- Original duck SVG/favicon/icon artwork: AGPL-3.0-or-later. Created from scratch
  for SHMAX; see [TRADEMARKS.md](TRADEMARKS.md). No third-party graphic inputs.

Do not remove upstream license notices from release distributions.
