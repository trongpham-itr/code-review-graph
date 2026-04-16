# Note

```cypher
MATCH (field:GQLField)<-[r1:RESOLVES|RESOLVES_EXTERNAL]-(resolver:Function)
WHERE field.operation IN ["mutation", "query"]
MATCH p = (resolver)-[:CALLS|DELEGATES_TO*1..8]->(target:Function {name: "getProfileWorker"})
RETURN field, r1, p
```

```cypher
MATCH p = 
  (:GQLField {name: "technicianComments"})
    -[:FIELD_OF]->
  (:GQLType {name: "HolterProfile"})
    <-[:RETURNS]-
  (:GQLField {name: "holterProfile", operation: "query"})
    <-[:RESOLVES]-
  (:Function)
    -[:DELEGATES_TO]->
  (:Function)
RETURN p
```

```cypher
MATCH p=({name: "listEcgBookmarks"})-[]-() RETURN p
```

- **DiagnosisCode -> fullDisplay**

```cypher
MATCH p=({name: "diagnosisCodes"})-[]-() RETURN p
```

- **Study -> info -> StudyInfo -> patient**

```cypher
MATCH p=({name: "clinicStudies"})-[]-() RETURN p
```

- **List files import `models/pdfreport`**

```cypher
MATCH (src:Node)-[:IMPORTS_FROM]->(target:Node)
WHERE toLower(target.file_path) CONTAINS 'models/pdfreport'
RETURN src.name, src.kind, src.repo, src.file_path, target.name
ORDER BY src.repo, src.file_path
```
