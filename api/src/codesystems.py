import json


class BaseCodeSystem(object):

    def __init__(self, filepath):
        with open(filepath, encoding="utf-8") as f:
            self.codesystem = json.load(f)
            self.TYPE_CODES = [{"code": concept["code"], "display": concept["display"]} for concept in self.codesystem["concept"]]

    @property
    def url(self):
        return self.codesystem.get('url')
    
    def display(self, code):
        _display = next((item["display"] for item in self.TYPE_CODES if item["code"] == code), None)
        return _display



type_codes = BaseCodeSystem("./codesystems/CodeSystem-Dokumententypen.json")
category_codes = BaseCodeSystem("./codesystems/CodeSystem-Dokumentenklassen.json")
event_codes = BaseCodeSystem("./codesystems/CodeSystem-DokumentenWarnhinweise.json")
facility_type_codes = BaseCodeSystem("./codesystems/CodeSystem-EinrichtungsartenPatientenbezogen.json")
practice_setting_codes = BaseCodeSystem("./codesystems/CodeSystem-FachrichtungenAerztlich.json")
