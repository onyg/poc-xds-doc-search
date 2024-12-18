import os
import shutil
import requests


# Warte auf Elasticsearch und HAPI, um verfügbar zu sein
ES_HOST = os.getenv("ELASTICSEARCH_HOST", "localhost")
ES_URL = f"http://{ES_HOST}:9200"
HAPI_HOST = os.getenv("HAPI_HOST", "localhost")
HAPI_URL = f"http://{HAPI_HOST}:8080/fhir/"
UPLOAD_FOLDER = './uploaded'

PATH_BASE = "/epa/mhd/api/v1/fhir"

timeout = 60  # Timeout in Sekunden


def check_service(url):
    try:
        response = requests.get(url)
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        return False
    

def create_uploaded_files():
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # Erstellt das Verzeichnis neu
        print(f"Verzeichnis '{UPLOAD_FOLDER}' wurde erstellt.")


def clear_uploaded_files():
    """Alle Dateien im angegebenen Verzeichnis löschen."""
    if os.path.exists(UPLOAD_FOLDER):
        shutil.rmtree(UPLOAD_FOLDER)  # Löscht das gesamte Verzeichnis
        os.makedirs(UPLOAD_FOLDER)    # Erstellt das Verzeichnis neu
        print(f"Alle Dateien im Verzeichnis '{UPLOAD_FOLDER}' wurden gelöscht.")
