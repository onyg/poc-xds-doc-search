import os
import uuid
import datetime
import re
import time

import requests

from flask import Flask, request, jsonify, Response
from zeep import Client, exceptions
from PyPDF2 import PdfReader
from elasticsearch import Elasticsearch
from fhir.resources.documentreference import DocumentReference
from fhir.resources.bundle import Bundle, BundleEntry, BundleEntrySearch
from fhir.resources.extension import Extension

app = Flask(__name__)

# Warte auf Elasticsearch, um verfügbar zu sein
es_host = os.getenv("ELASTICSEARCH_HOST", "localhost")
es_url = f"http://{es_host}:9200"
timeout = 60  # Timeout in Sekunden

print("Warte auf Elasticsearch, um verfügbar zu sein...")
for _ in range(timeout):
    try:
        response = requests.get(f"{es_url}/_cluster/health")
        if response.status_code == 200:
            print("Elasticsearch ist betriebsbereit.")
            break
    except requests.exceptions.ConnectionError:
        pass
    time.sleep(1)
else:
    print("Elasticsearch ist nach 60 Sekunden nicht verfügbar. Beende das Skript.")
    exit(1)

# Elasticsearch-Client erstellen
es = Elasticsearch([es_url])

INDEX_NAME = "xds_documents"

# Erstellen des Indexes in Elasticsearch (falls noch nicht vorhanden)
if not es.indices.exists(index=INDEX_NAME):
    es.indices.create(index=INDEX_NAME, body={
        "mappings": {
            "properties": {
                "id": {"type": "keyword"},
                "type": {"type": "text"},
                "title": {"type": "text"},
                "author": {"type": "text"},
                "date": {"type": "date"},
                "content": {"type": "text"}
            }
        }
    })

@app.route('/upload', methods=['POST'])
def upload_document():
    """Hochladen einer PDF-Datei, Analysieren des Inhalts und Indizieren für die Volltextsuche."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    iso_date = datetime.datetime.now().replace(microsecond=0).isoformat() + "Z"  # Z fügt das UTC-Zeichen hinzu

    # PDF-Inhaltsanalyse
    try:
        pdf_reader = PdfReader(file)
        pdf_text = ""
        for page in pdf_reader.pages:
            pdf_text += page.extract_text() + "\n"

        # Dokument-Metadaten
        doc_id = str(uuid.uuid4())
        document_reference = DocumentReference(
            id=doc_id,
            status="current",
            docStatus="final",
            type={
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "34133-9",
                        "display": "Summary of episode note"
                    }
                ]
            },
            date=iso_date,  # Korrekt formatiertes Datum verwenden
            author=[
                {
                    "display": "Unknown Author"
                }
            ],
            content=[
                {
                    "attachment": {
                        "contentType": "application/pdf",
                        "url": f"http://localhost:5000/files/{doc_id}.pdf",
                        "title": file.filename,
                        "creation": iso_date  # Auch hier das korrekte Format verwenden
                    }
                }
            ]
        )

        # Dokument in Elasticsearch indizieren
        es.index(index=INDEX_NAME, id=doc_id, body={
            "id": doc_id,
            "type": "DocumentReference",
            "title": file.filename,
            "author": "Unknown Author",
            "date": document_reference.date,
            "content": pdf_text
        })

        return jsonify(document_reference.dict()), 201

    except Exception as e:
        return jsonify({"error": f"Failed to process PDF: {str(e)}"}), 500

@app.route('/$search', methods=['POST'])
def search_documents():
    """Volltextsuche in den indizierten Dokumenten durchführen und alle Treffer in einem Dokument anzeigen."""
    query = request.json.get("query")

    es_query = {
        "query": {
            "match_phrase": {
                "content": query
            }
        },
        "highlight": {
            "fields": {
                "content": {
                    "pre_tags": ["<mark>"],
                    "post_tags": ["</mark>"]
                }
            }
        }
    }

    try:
        res = es.search(index="xds_documents", body=es_query)
        entries = []

        for hit in res['hits']['hits']:
            matched_snippets = []
            if 'highlight' in hit and 'content' in hit['highlight']:
                # Bereinige alle Treffer von Newlines und Tags und speichere sie im Array
                for snippet in hit['highlight']['content']:
                    clean_snippet = snippet.replace('\n', ' ').replace('\r', ' ').strip()
                    # clean_snippet = re.sub(r'<.*?>', '', clean_snippet)
                    matched_snippets.append(clean_snippet)

            score = hit['_score']

            document_reference = DocumentReference(
                id=hit['_id'],
                status="current",
                docStatus="final",
                type={
                    "coding": [
                        {
                            "system": "http://loinc.org",
                            "code": "34133-9",
                            "display": "Summary of episode note"
                        }
                    ]
                },
                date=hit['_source'].get('date'),
                author=[
                    {
                        "display": hit['_source'].get('author')
                    }
                ],
                content=[
                    {
                        "attachment": {
                            "contentType": "application/pdf",
                            "url": f"http://localhost:5000/files/{hit['_id']}.pdf",
                            "title": hit['_source'].get('title'),
                            "creation": hit['_source'].get('date')
                        }
                    }
                ]
            )

            # Füge alle bereinigten Treffer in eine Extension ein
            extensions = [
                Extension(
                    url="http://example.org/fhir/StructureDefinition/matchSnippet",
                    valueString=snippet
                ) for snippet in matched_snippets
            ]

            search_entry = BundleEntrySearch(
                mode="match",
                score=score,
                extension=extensions if matched_snippets else []
            )

            entry = BundleEntry(
                fullUrl=f"urn:uuid:{hit['_id']}",
                resource=document_reference,
                search=search_entry
            )
            entries.append(entry)

        bundle = Bundle(
            type="searchset",
            total=len(entries),
            entry=entries
        )

        response = Response(
            response=bundle.json(),
            status=200,
            mimetype='application/json'
        )
        return response

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8001)
