import requests
import json
import re
import logging
from collections import OrderedDict

from config import HAPI_URL, PATH_BASE


def put_documentreference(file_id, document_reference):
    headers = {'Content-Type': 'application/fhir+json'}
    response = requests.put(f"{HAPI_URL}/DocumentReference/{file_id}", json=document_reference.dict(), headers=headers)
    if response.status_code not in [200, 201]:
        return False
    return True


class FHIRClient(object):
    HEADERS = {'Content-Type': 'application/fhir+json'}

    def get(self, resource_type, resource_id):
        response = requests.get(f"{HAPI_URL}{resource_type}/{resource_id}")
        json_response = json.loads(response.content, object_pairs_hook=OrderedDict)
        return json_response, response.status_code, response.headers['Content-Type']

    def search(self, host, resource_type, search_params):
        if '_offset' not in search_params:
            search_params['_offset'] = ['0']
        response = requests.get(HAPI_URL + resource_type, params=search_params)
        json_response = json.loads(response.content, object_pairs_hook=OrderedDict)
        if response.status_code == 200:
            # Überprüfe, ob "link" vorhanden ist
            if 'link' in json_response:
                for link_item in json_response['link']:
                    if link_item.get('url'):
                        # Suche nach resource_type in der URL
                        match = re.search(r'/{}.*'.format(resource_type), link_item['url'])
                        if match:
                            # Ersetze alles vor resource_type durch /epa/medication/api/v1
                            link_item['url'] = f"{PATH_BASE}{match.group(0)}"
            if 'entry'in json_response:
                for entry in json_response['entry']:
                    if entry.get('fullUrl'):
                        entry_resource = entry.get('resource')
                        if entry_resource:
                            entry_resource_type = entry_resource.get('resourceType')
                            if entry_resource_type:
                                # Suche nach resource_type in der URL
                                match = re.search(r'/{}.*'.format(entry_resource_type), entry['fullUrl'])
                                if match:
                                    # Ersetze alles vor resource_type durch /epa/medication/api/v1
                                    entry['fullUrl'] = f"http://{host}{PATH_BASE}{match.group(0)}"
        
        return json_response, response.status_code, response.headers['Content-Type']
    
    def update(self, resource_type, id, resource):
        response = requests.put(f"{HAPI_URL}/{resource_type}/{id}", json=resource.dict(), headers=self.HEADERS)
        if response.status_code not in [200, 201]:
            return False
        return True
    
    def delete_all(self, resource_type):
        try:
            resources = self.find_all(resource_type=resource_type)
            for resource in resources:
                _id = resource.get('id', '0')
                delete_url = f"{HAPI_URL}{resource_type}/{_id}?_expunge=true"
                response = requests.delete(delete_url)
            if response.status_code in [200, 204]:
                logging.info(f"Resource mit ID {_id} erfolgreich gelöscht und expunged.")
            else:
                logging.error(f"Fehler beim Löschen von ID {_id}: {response.text}")

        except Exception as e:
            logging.exception(f"Ein Fehler ist aufgetreten: {e}")
    
    def find_all(self, resource_type, search_params={}):
        resources = []
        if '_count' in search_params: del search_params['_count']
        url = f"{HAPI_URL}{resource_type}?_count=1000"
        for key, value_list in search_params.items():
            for value in value_list:
                url = f"{url}&{key}={value}"
        while url:
            response = requests.get(url)
            if response.status_code != 200:
                logging.error(f"Fehler bei der Suche: {response.text}")
                return []
            data = response.json()
            entries = data.get('entry', [])
            for entry in entries:
                resources.append(entry['resource'])
            logging.info(f"{len(entries)} Resources gefunden.")
            # Nächste Seite abrufen, falls vorhanden
            url = None
            for link in data.get('link', []):
                if link['relation'] == 'next':
                    url = link['url']
                    break
        return resources


fhir_client = FHIRClient()