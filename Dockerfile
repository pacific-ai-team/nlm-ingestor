# syntax=docker/dockerfile:experimental
FROM python:3.11-bookworm

# Update and upgrade system packages to get the latest security fixes
RUN apt-get update && apt-get upgrade -y && \
  apt-get install -y --no-install-recommends libgomp1 && \
  rm -rf /var/lib/apt/lists/*

ENV APP_HOME /root
ENV PYTHONPATH="${PYTHONPATH}:${APP_HOME}"
ENV PYTHONUNBUFFERED=1

# Add testing repository for newer libxml2
RUN echo "deb http://deb.debian.org/debian testing main" > /etc/apt/sources.list.d/testing.list && \
  echo 'APT::Default-Release "bookworm";' > /etc/apt/apt.conf.d/99default-release

# Update system packages to get latest security fixes
RUN apt-get update && \
  apt-get upgrade -y && \
  apt-get install -y --no-install-recommends \
  curl \
  gnupg \
  lsb-release \
  sqlite3 \
  libopenjp2-7 && \
  # Now install libxml2 from testing repo
  apt-get install -y -t testing libxml2 && \
  apt-get clean && \
  # Add PostgreSQL repository (using the modern approach)
  mkdir -p /etc/apt/keyrings && \
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /etc/apt/keyrings/postgresql.asc && \
  echo "deb [signed-by=/etc/apt/keyrings/postgresql.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
  apt-get update && \
  # Now install PostgreSQL client
  apt-get install -y postgresql-client-15 && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

# install Java
RUN mkdir -p /usr/share/man/man1 && \
  apt-get update -y && \
  apt-get upgrade -y && \
  apt-get install -y openjdk-17-jre-headless

# install essential packages
RUN apt-get update && apt-get install -y \
  -t testing libxml2-dev libxslt-dev \
  build-essential libmagic-dev && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

# install tesseract and related dependencies
RUN apt-get update && apt-get install -y \
  tesseract-ocr lsb-release ca-certificates gnupg wget && \
  wget -q -O - https://notesalexp.org/debian/alexp_key.asc | apt-key add - && \
  echo "deb https://notesalexp.org/tesseract-ocr5/bookworm/ bookworm main" > /etc/apt/sources.list.d/notesalexp.list && \
  apt-get update && \
  apt-get install -y tesseract-ocr libtesseract-dev && \
  wget -P /usr/share/tesseract-ocr/5/tessdata/ \
  https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata && \
  # Clean up testing repository to prevent future conflicts
  rm -f /etc/apt/sources.list.d/testing.list && \
  apt-get update && \
  # Fix any broken packages
  apt-get -f install -y && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

# After installing tesseract, clean up testing artifacts and fix package states
RUN apt-get update && \
  apt-get clean && \
  # Explicitly downgrade/pin libmagic-mgc to match libmagic1 from stable
  apt-get install -y --allow-downgrades \
  libmagic-mgc=1:5.44-3 \
  libmagic1=1:5.44-3 && \
  rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y unzip git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR ${APP_HOME}
RUN mkdir -p /${APP_HOME}/whl
COPY whl/*.whl /root/whl/
COPY pyproject.toml poetry.lock ./
RUN pip install poetry && \
  poetry config virtualenvs.create false && \
  poetry install --no-root

COPY . ./

RUN mkdir -p -m 0600 ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts
RUN python -m nltk.downloader stopwords
RUN python -m nltk.downloader punkt
RUN python -m nltk.downloader punkt_tab
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
RUN chmod +x run.sh

EXPOSE 5001
# CMD ./run.sh