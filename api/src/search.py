import re

from elasticsearch import Elasticsearch

from config import ES_URL


INDEX_NAME = "xds_documents"


class ElasticsearchClient(object):
    _es_client = None

    @property
    def client(self):
        if self._es_client is None:
            self._es_client = Elasticsearch([ES_URL])
        return self._es_client
    
    def create_index(self):
        # Erstellen des Indexes in Elasticsearch (falls noch nicht vorhanden)

        if not self.client.indices.exists(index=INDEX_NAME):
            self.client.indices.create(index=INDEX_NAME, body={
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

    def delete_index(self):
        self.client.indices.delete(index=INDEX_NAME)

    def index(self, id, content):
        self.client.index(index=INDEX_NAME, id=id, body=content)

    def parse_expression(self, expression):
        """Parst den logischen Ausdruck und gibt eine Elasticsearch-Query zurück."""
        
        def process_tokens(tokens):
            """Rekursiv Tokens verarbeiten und Elasticsearch-Queries erstellen."""
            stack = []
            current = {"bool": {"must": []}}  # Standard-AND für den äußeren Block
            
            while tokens:
                token = tokens.pop(0)

                if token == "(":
                    # Rekursiver Aufruf für Sub-Expression
                    sub_expression = process_tokens(tokens)
                    stack.append(sub_expression)
                elif token == ")":
                    # Block beenden
                    break
                elif token.upper() == "AND":
                    stack.append("AND")
                elif token.upper() == "OR":
                    stack.append("OR")
                elif token.startswith('"') and token.endswith('"'):
                    # Exakter Wert
                    stack.append({"term": {"content.keyword": token.strip('"')}})
                else:
                    # Fuzzy oder Teilwortsuche
                    stack.append({"match": {"content": {"query": token, "fuzziness": "AUTO"}}})

            # Baue die Elasticsearch-Query aus dem Stack
            query = build_query_from_stack(stack)
            return query

        def build_query_from_stack(stack):
            """Erstellt eine Elasticsearch-Query aus dem Stack."""
            if not stack:
                return {}

            must_clauses = []
            should_clauses = []
            current_operator = "AND"

            for item in stack:
                if item == "AND":
                    current_operator = "AND"
                elif item == "OR":
                    current_operator = "OR"
                else:
                    if current_operator == "AND":
                        must_clauses.append(item)
                    elif current_operator == "OR":
                        should_clauses.append(item)

            if should_clauses:
                return {"bool": {"should": should_clauses, "minimum_should_match": 1}}
            return {"bool": {"must": must_clauses}}

        # Tokenisiere den Ausdruck
        tokens = re.findall(r'\(|\)|"[^"]+"|\bAND\b|\bOR\b|\w+', expression)
        return process_tokens(tokens)

    
    def build_elasticsearch_query(self, fulltext_expressions):
        """Kombiniert mehrere _fulltext-Expressions mit AND."""
        must_conditions = []

        for expression in fulltext_expressions:
            query = self.parse_expression(expression)
            must_conditions.append(query)

        return {"bool": {"must": must_conditions}}

    def search(self, _ids, search_terms):

        # Elasticsearch-Query erstellen
        query = self.build_elasticsearch_query(search_terms)

        # ResourceId-Filter hinzufügen
        query["bool"]["must"].append({
            "terms": {
                "id": _ids
            }
        })

        # Elasticsearch-Suche ausführen mit <mark> Tag Highlights
        response = self.client.search(
            index=INDEX_NAME,
            body={
                "query": query,
                "highlight": {
                    "fields": {
                        "content": {
                            "pre_tags": ["<mark>"],
                            "post_tags": ["</mark>"]
                        }
                    }
                }
            }
        )

        return response


es_client = ElasticsearchClient()