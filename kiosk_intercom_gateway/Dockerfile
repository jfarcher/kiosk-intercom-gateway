ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY src/kig /app/kig
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# s6-overlay resets the environment of the CMD; with-contenv restores the
# container's, which carries SUPERVISOR_TOKEN.
CMD ["/command/with-contenv", "python3", "-m", "kig"]
