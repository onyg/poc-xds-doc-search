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
    """PDF-Datei hochladen und seitenweise in Elasticsearch indizieren."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        file_id = str(uuid.uuid4())
        pdf_reader = PdfReader(file)
        pages = []

        for page_num, page in enumerate(pdf_reader.pages):
            page_text = page.extract_text()
            if page_text:
                # Jede Seite als separates Dokument in Elasticsearch speichern
                es.index(index="xds_documents", id=f"{file_id}_page_{page_num + 1}", body={
                    "file_id": file_id,
                    "page_number": page_num + 1,
                    "content": page_text.strip(),
                    "filename": file.filename
                })
                pages.append(page_num + 1)

        return jsonify({
            "message": "File uploaded and indexed successfully",
            "file_id": file_id,
            "pages_indexed": pages,
            "filename": file.filename
        }), 201

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/$search', methods=['POST'])
def search_documents():
    """Durchsuche alle hochgeladenen PDFs in Elasticsearch nach einem bestimmten Suchbegriff."""
    query = request.json.get("query")

    if not query:
        return jsonify({"error": "Missing query"}), 400

    es_query = {
        "query": {
            "match": {
                "content": {
                    "query": query,
                    "fuzziness": "AUTO"
                }
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
        entries = {}
        
        for hit in res['hits']['hits']:
            file_id = hit['_source']['file_id']
            page_number = hit['_source']['page_number']
            filename = hit['_source'].get('filename', 'Unknown')
            score = hit['_score']
            matched_snippet = []

            if 'highlight' in hit and 'content' in hit['highlight']:
                for snippet in hit['highlight']['content']:
                    # clean_snippet = re.sub(r'<.*?>', '', snippet)
                    clean_snippet = snippet.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').strip()
                    matched_snippet.append(f"Page {page_number}: {clean_snippet}")

            if file_id not in entries:
                document_reference = DocumentReference(
                    id=file_id,
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
                    content=[
                        {
                            "attachment": {
                                "contentType": "application/pdf",
                                "title": filename
                            }
                        }
                    ]
                )

                entries[file_id] = {
                    "document_reference": document_reference,
                    "matched_snippets": []
                }

            entries[file_id]["matched_snippets"].extend(matched_snippet)

        bundle_entries = []
        for file_id, entry_data in entries.items():
            extensions = [
                Extension(
                    url="http://example.org/fhir/StructureDefinition/matchSnippet",
                    valueString=snippet
                ) for snippet in entry_data["matched_snippets"]
            ]

            search_entry = BundleEntrySearch(
                mode="match",
                extension=extensions,
                score=score
            )

            bundle_entry = BundleEntry(
                fullUrl=f"urn:uuid:{file_id}",
                resource=entry_data["document_reference"],
                search=search_entry
            )
            bundle_entries.append(bundle_entry)

        bundle = Bundle(
            type="searchset",
            total=len(bundle_entries),
            entry=bundle_entries
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
