import os
import uuid
import time
import json

from urllib.parse import urlparse, parse_qs, urlencode

from flask import Flask, request, jsonify, Response, send_file, render_template
from PyPDF2 import PdfReader
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.bundle import Bundle, BundleEntrySearch
from fhir.resources.R4B.extension import Extension

from config import check_service, clear_uploaded_files, create_uploaded_files, ES_URL, HAPI_URL, UPLOAD_FOLDER, PATH_BASE
from search import es_client
from fhirapi import put_documentreference, fhir_client
from codesystems import type_codes, event_codes, category_codes, facility_type_codes, practice_setting_codes


app = Flask(__name__)

timeout = 60  # Timeout in Sekunden

print("Warte auf Elasticsearch und HAPI, um verfügbar zu sein...")
for _ in range(timeout):
    es_ready = check_service(f"{ES_URL}/_cluster/health")
    hapi_ready = check_service(f"{HAPI_URL}/DocumentReference")

    if es_ready and hapi_ready:
        print("Elasticsearch und HAPI sind betriebsbereit.")
        break
    
    time.sleep(1)
else:
    print("Elasticsearch und HAPI ist nach 60 Sekunden nicht verfügbar. Beende das Skript.")
    exit(1)


app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
es_client.create_index()
create_uploaded_files()


@app.route(PATH_BASE + "/<resource_type>", methods=["GET"])
def search_resource(resource_type):
    query_params = {key: request.args.getlist(key) for key in request.args if key != '_fulltext'}
    fulltext_params = request.args.getlist('_fulltext')
    existing_ids = query_params.get('_id', [])
    es_response = None

    if fulltext_params:
        resources = fhir_client.find_all(resource_type=resource_type, search_params=query_params)
        resource_ids = [resource['id'] for resource in resources]

        es_response = es_client.search(_ids=resource_ids, search_terms=fulltext_params)
        es_ids = list(set([hit['_source']['id'] for hit in es_response['hits']['hits']]))
        
        if es_ids:
            combined_ids = set(existing_ids + [",".join(es_ids)])
            query_params['_id'] = [",".join(combined_ids)]
        else:
            query_params['_id'] = ["-1"]

    # FHIR-Suche
    content, status_code, content_type = fhir_client.search(
        host=request.host, 
        resource_type=resource_type, 
        search_params=query_params
    )
    if status_code not in [200]:
        return Response(
            content,
            status=status_code,
            content_type=content_type
        )

    # CleanUp Links im searchset FHIR-Bundle (_id-Parameter entfernen)
    if 'link' in content:
        for link in content['link']:
            href = link.get('url')
            if href:
                parsed_url = urlparse(href)
                query = parse_qs(parsed_url.query)
                query.pop('_id', None)
                if existing_ids:
                    query['_id'] = existing_ids
                if fulltext_params:
                    query['_fulltext'] = fulltext_params
                new_query = urlencode(query, doseq=True)
                new_href = parsed_url._replace(query=new_query).geturl()
                link['url'] = new_href

    bundle = Bundle(**content)

    if es_response:
        max_score = es_response['hits']['max_score']
        if bundle.entry:
            for entry in bundle.entry:
                _id = entry.resource.id
                for hit in es_response['hits']['hits']:
                    if entry.resource.id == hit['_source']['id']:
                        page_number = hit['_source']['page_number']
                        score = hit['_score'] / max_score if max_score > 0 else 0
                        matched_snippet = []
                        if 'highlight' in hit and 'content' in hit['highlight']:
                            for snippet in hit['highlight']['content']:
                                clean_snippet = snippet.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').strip()
                                matched_snippet.append((page_number, clean_snippet))
                        extensions = [
                            Extension(
                                url="https://gematik.de/fhir/mhd/StructureDefinition/epa-match-snippet",
                                extension=[
                                    Extension(url="snippet", valueString=snippet),
                                    Extension(url="pageNumber", valueString=page)
                                ]
                            ) for page, snippet in matched_snippet
                        ]

                        entry.search = BundleEntrySearch(
                            mode="match",
                            extension=extensions,
                            score=score
                        )

    return Response(
        bundle.json(),
        status=status_code,
        content_type=content_type
    )


@app.route(PATH_BASE + "/<resource_type>/<resource_id>", methods=["GET"])
def get_instance(resource_type, resource_id):
    # FHIR-GET
    content, status_code, content_type = fhir_client.get(
        resource_type=resource_type, 
        resource_id=resource_id
    )
    return Response(
        json.dumps(content),
        status=status_code,
        content_type=content_type
    )


@app.route('/delete', methods=['DELETE', 'GET'])
def delete_index():
    es_client.delete_index()
    clear_uploaded_files()
    fhir_client.delete_all(resource_type='DocumentReference')
    es_client.create_index()
    create_uploaded_files()
    return jsonify({"deleted": "true"}), 200


@app.route('/upload', methods=['POST', 'GET'])
def upload():
    if request.method == 'POST':
        """PDF-Datei hochladen und seitenweise in Elasticsearch indizieren."""
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        try:
            file_id = str(uuid.uuid4())
            # Datei speichern
            file_extension = os.path.splitext(file.filename)[1] 
            saved_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{file_extension}")
            file.save(saved_path)

            # Index PDF
            pdf_reader = PdfReader(saved_path)
            pages = []
            for page_num, page in enumerate(pdf_reader.pages):
                page_text = page.extract_text()
                if page_text:
                    # Jede Seite als separates Dokument in Elasticsearch speichern
                    es_client.index(id=f"{file_id}_page_{page_num + 1}", content={
                        "id": file_id,
                        "page_number": page_num + 1,
                        "content": page_text.strip(),
                        "filename": file.filename
                    })
                    pages.append(page_num + 1)

            # Save FHIR DocumentReference
            content_url = f"http://{request.host}/epa/mhd/retrieve/v1/content/{file_id}{file_extension}"
            document_reference = DocumentReference(
                id=file_id,
                meta={
                    "profile":  [
                        "https://gematik.de/fhir/mhd/StructureDefinition/epa-mhd-document-reference"
                     ]
                },
                subject= {
                    "identifier": {
                        "type": {
                            "coding":  [
                                {
                                    "system": "http://fhir.de/CodeSystem/identifier-type-de-basis",
                                    "code": "KVZ10",
                                    "display": "Krankenversichertennummer"
                                }
                            ]
                        },
                        "system": "http://fhir.de/sid/gkv/kvid-10",
                        "value": request.form.get('subject')
                    },
                    "type": "Patient"
                },
                identifier=[
                    {
                        "system": "urn:ietf:rfc:3986",
                        "use": "official",
                        "value": f"urn:oid:{file_id}"
                    }
                ],
                masterIdentifier={
                    "system": "urn:ietf:rfc:3986",
                    "value": f"urn:oid:{file_id}"
                },
                status=request.form.get('status'),
                type={
                    "coding": [
                        {
                            "system": "urn:oid:1.3.6.1.4.1.19376.3.276.1.5.9",
                            "code": request.form.get('type'),
                            "display": type_codes.display(request.form.get('type'))
                        }
                    ]
                },
                category=[
                    {
                        "coding":  [
                            {
                                "system": "urn:oid:1.3.6.1.4.1.19376.3.276.1.5.8",
                                "code": request.form.get('category'),
                                "display": category_codes.display(request.form.get('category'))
                            }
                        ]
                    }
                ],
                author=[
                    {
                        "type": "Organization",
                        "identifier": {
                            "system": "https://gematik.de/fhir/sid/telematik-id",
                            "value": request.form.get('author')
                        }
                    }
                ],
                description=request.form.get('description'),
                content=[
                    {
                        "attachment": {
                            "contentType": "application/pdf",
                            "language": "de-DE",
                            "title": file.filename,
                            "url": content_url
                        },
                        "format": {
                            "code": "urn:ihe:iti:xds:2017:mimeTypeSufficient",
                            "system": "urn:oid:1.3.6.1.4.1.19376.1.2.3",
                            "display": "Format aus MIME Type ableitbar"
                        }
                    }
                ],
                context={
                    "event":  [
                        {
                            "coding":  [
                                {
                                    "system": "urn:oid:1.3.6.1.4.1.19376.3.276.1.5.15",
                                    "code": request.form.get('event'),
                                    "display": event_codes.display(request.form.get('event'))
                                }
                            ]
                        }
                    ],
                    "period": {
                        "start": "2024-11-28"
                    },
                    "facilityType": {
                        "coding":  [
                            {
                                "system": "urn:oid:1.3.6.1.4.1.19376.3.276.1.5.2",
                                "code": request.form.get('facilityType'),
                                "display": facility_type_codes.display(request.form.get('facilityType'))
                            }
                        ]
                    },
                    "practiceSetting": {
                        "coding":  [
                            {
                                "system": "urn:oid:1.3.6.1.4.1.19376.3.276.1.5.4",
                                "code": request.form.get('practiceSetting'),
                                "display": practice_setting_codes.display(request.form.get('practiceSetting'))
                            }
                        ]
                    },
                }
            )
            put_documentreference(file_id=file_id, document_reference=document_reference)
            instance = json.dumps(json.loads(document_reference.json()), sort_keys = False, indent = 4)
            return render_template('form.html', document_reference=instance)
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return render_template('form.html',
                           type_codes=type_codes.TYPE_CODES,
                           category_codes=category_codes.TYPE_CODES,
                           event_codes=event_codes.TYPE_CODES,
                           facility_type_codes=facility_type_codes.TYPE_CODES,
                           practice_setting_codes=practice_setting_codes.TYPE_CODES)


@app.route('/epa/mhd/retrieve/v1/content/<file_id>', methods=['GET'])
def download_file(file_id):
    """Gespeicherte Datei anhand der file_id herunterladen."""
    try:
        # Datei im Upload-Ordner suchen
        saved_path = os.path.join(app.config['UPLOAD_FOLDER'])
        for filename in os.listdir(saved_path):
            if filename.startswith(file_id):
                filepath = os.path.join(saved_path, filename)
                return send_file(
                    filepath,
                    as_attachment=True
                )
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8001)
