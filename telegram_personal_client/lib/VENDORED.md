# Bundled libraries

Pure-Python libraries shipped inside the module so it installs without
`pip`. Loaded by `engine/vendor.py`. A library already installed on the
server is used instead for `pyasn1`; the others always come from here, so
the module runs the exact versions it was tested with.

| Library | Version | License | Source (sha256 of the downloaded file) |
|---|---|---|---|
| Telethon | 1.45.0 | MIT | telethon-1.45.0-py3-none-any.whl `339e24c83aedc9f12cbe389264627edc49779e4289c535aa90aafc4ce62f7972` |
| pyaes | 1.6.1 | MIT | pyaes-1.6.1.tar.gz `02c1b1405c38d3c370b085fb952dd8bea3fadcee6411ad99f312cc129c536d8f` |
| rsa | 4.9.1 | Apache-2.0 | rsa-4.9.1-py3-none-any.whl `68635866661c6836b8d39430f97a996acbd61bfa49406748ea243539fe239762` |
| pyasn1 | 0.6.4 | BSD-2-Clause | pyasn1-0.6.4-py3-none-any.whl `deda9277cfd454080ec40b207fb6df82206a3a2688735233cdcd8d3d565f088b` |

License texts are in `LICENSES/`. Files are unmodified copies of the
package sources from PyPI.
