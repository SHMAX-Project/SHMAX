# ODROID-XU4 / ARMv7 (armhf)

**Experimental designed target. Physical ARMv7/ODROID-XU4 acceptance is incomplete. Only Debian 13 amd64 has owner-confirmed live acceptance.**

Debian 13/Armbian on ODROID-XU4 (2 GiB). Build packages:
zlib1g-dev, libjpeg62-turbo-dev, libpng-dev, libtiff-dev,
libwebp-dev, libfreetype6-dev, liblcms2-dev. Runtime:
libjpeg62-turbo, libpng16-16t64, libtiff6, libwebp7,
libfreetype6, liblcms2-2, zlib1g (exact names matter — do NOT use
libwebp7t64-style guesses). Avoid casual `apt autoremove` on
Armbian. ARM image builds are slow — that is normal.
