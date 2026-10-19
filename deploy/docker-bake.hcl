# ============================================================
# SHMAX docker buildx bake — multi-architecture
#
# Targets: linux/amd64 (production), linux/arm64 (production),
#          linux/arm/v7 (experimental).
#
# Registry is a placeholder — set SHMAX_REGISTRY env var or
# override on the command line:
#
#   SHMAX_REGISTRY=ghcr.io/shmax docker buildx bake \
#     --push --set "*.tags=latest" image
#
# The bake file is intentionally declarative so release
# engineers can audit which architectures we ship.
# ============================================================

variable "SHMAX_REGISTRY" {
  default = "registry.example.invalid/shmax"
}

variable "SHMAX_IMAGE" {
  default = "shmax"
}

variable "SHMAX_VERSION" {
  default = "0.1.2"
}

variable "PLATFORMS_AMD64" {
  default = "linux/amd64"
}

variable "PLATFORMS_ARM64" {
  default = "linux/arm64"
}

variable "PLATFORMS_ARMV7" {
  default = "linux/arm/v7"
}

# Stable + experimental split: amd64 + arm64 are supported in
# V1; armv7 builds but is marked experimental.
group "default" {
  targets = ["image-amd64", "image-arm64"]
}

group "experimental" {
  targets = ["image-amd64", "image-arm64", "image-armv7"]
}

target "common" {
  context    = "."
  dockerfile = "Dockerfile"
  args = {
    SHMAX_VERSION = "${SHMAX_VERSION}"
  }
  annotations = [
    "org.opencontainers.image.title=SHMAX",
    "org.opencontainers.image.description=MAX <-> Telegram bridge",
    "org.opencontainers.image.source=",
    "org.opencontainers.image.version=${SHMAX_VERSION}",
  ]
}

target "image-amd64" {
  inherits = ["common"]
  platforms = ["${PLATFORMS_AMD64}"]
  tags = [
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:${SHMAX_VERSION}-amd64",
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:amd64",
  ]
}

target "image-arm64" {
  inherits = ["common"]
  platforms = ["${PLATFORMS_ARM64}"]
  tags = [
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:${SHMAX_VERSION}-arm64",
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:arm64",
  ]
}

target "image-armv7" {
  inherits = ["common"]
  platforms = ["${PLATFORMS_ARMV7}"]
  tags = [
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:${SHMAX_VERSION}-armv7",
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:armv7",
  ]
  # armv7 cannot run python:3.12-slim's glibc wheels for some
  # deps. Experimental — V1 does not promise armv7 production
  # support.
}

# A multi-arch "manifest" target that produces an image
# reference covering all supported architectures. Useful for
# `docker buildx bake --push image` to ship amd64+arm64 in
# one tag.
target "image" {
  inherits = ["common"]
  platforms = [
    "${PLATFORMS_AMD64}",
    "${PLATFORMS_ARM64}",
  ]
  tags = [
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:${SHMAX_VERSION}",
    "${SHMAX_REGISTRY}/${SHMAX_IMAGE}:latest",
  ]
}
