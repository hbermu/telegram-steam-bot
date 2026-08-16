# Telegram Steam Bot — everything Steam-related, served from k3s.
#
# The captures are NOT in this image and NOT in a volume: they stay on the
# console and are read over SSH per request, then deleted. That is why ssh and
# rsync are installed here and why there is no storage anywhere in the manifest.
FROM python:3.13-slim

# ffmpeg does every image and video operation — there is no ImageMagick and none
# is wanted.
#
# fontconfig and a font are NOT optional. media._font() resolves the label font
# with `fc-match sans`, and when nothing resolves, drawtext is skipped *silently*:
# the clips still encode, they just lose the per-day label.
#
# openssh-client and rsync are the capture transport. The package is standard
# library only, so it shells out rather than taking a dependency on paramiko.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fontconfig \
        fonts-noto-core \
        openssh-client \
        rsync \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY steam_bot/ /app/steam_bot/
COPY selftest.py /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Unprivileged. The SSH key arrives as a Secret volume owned by root, which this
# user cannot read at 0400 and which OpenSSH refuses at 0440 — source.py stages a
# private 0600 copy at startup to settle that.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin bot
USER 1000

ENTRYPOINT ["python3", "-m", "steam_bot"]
