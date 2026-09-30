# MinIO no longer publishes images (quay.io and Docker Hub refuse every tag), so this builds the
# server and mc from their AGPLv3 source, unmodified, at pinned release tags. Each clone must be at
# the tag's commit, and go.sum checks every module. The ldflags follow upstream's release build, so
# `minio --version` and `mc --version` report the release and commit like the published binaries.
# To bump: change a tag and its commit (`git ls-remote <repo> 'refs/tags/<tag>^{}'`), and the
# image tag in infra/compose.yaml.
FROM golang:1.24.6-bookworm AS build
ARG MINIO_TAG=RELEASE.2025-09-07T16-13-09Z
ARG MINIO_COMMIT=07c3a429bfed433e49018cb0f78a52145d4bedeb
ARG MC_TAG=RELEASE.2025-08-13T08-35-41Z
ARG MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN <<'EOF'
set -eu
build() {  # repo, tag, commit
  git clone -q --depth 1 --branch "$2" "https://github.com/minio/$1" "/src/$1"
  cd "/src/$1"
  [ "$(git rev-parse HEAD)" = "$3" ] || { echo "$1 $2 is not at $3" >&2; exit 1; }
  # gen-ldflags takes the tag's time with colons: RELEASE.2025-09-07T16-13-09Z -> 2025-09-07T16:13:09Z
  ver=$(echo "${2#RELEASE.}" | sed -E 's/T([0-9]{2})-([0-9]{2})-([0-9]{2})Z$/T\1:\2:\3Z/')
  flags=$(MINIO_RELEASE=RELEASE MC_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "$ver")
  go build -tags kqueue -trimpath -ldflags "$flags" -o "/out/bin/$1"
  mkdir -p "/out/licenses/$1" && cp LICENSE CREDITS NOTICE "/out/licenses/$1/"
}
build minio "$MINIO_TAG" "$MINIO_COMMIT"
build mc "$MC_TAG" "$MC_COMMIT"
EOF

# bash for minio-init's script; mc for the healthcheck (`mc ready local`) and minio-init. Runs as
# root, like upstream's image, so the existing `minio` volume stays readable.
FROM debian:12.12-slim
COPY --from=build /out/bin/ /usr/bin/
COPY --from=build /out/licenses/ /licenses/
EXPOSE 9000
ENTRYPOINT ["minio"]
