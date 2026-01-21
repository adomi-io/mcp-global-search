### MCP Global Search — Indexes, Tools, and Collections

Generated: 2026-01-21 08:40 (local)

#### Overview
This report enumerates the available Meilisearch-backed MCP servers, their tools, the indexes they expose, and any declared collections with descriptions. It also runs a tiny sample search (`"introduction"`, limit 2) per index to validate access and provide quick evidence of content.

---

### Available MCP Servers and Tools

- Server: mcp_all-test
  - Tools:
    - `mcp_all-test_list_document_indexes(indexes)`: list available indexes with metadata and collections
    - `mcp_all-test_search_documents(uid, q, limit, offset)`: search a single index
    - `mcp_all-test_search_all_documents(...)`: multi-search across multiple indexes (not used here)
    - `mcp_all-test_get_document_file(path)`: fetch ground-truth source text for an item (not used here)

- Server: mcp_odoo-test
  - Tools:
    - `mcp_odoo-test_list_document_indexes(indexes)`: list available indexes
    - `mcp_odoo-test_search_documents(uid, q, limit, offset)`: search a single index
    - `mcp_odoo-test_search_all_documents(...)`: multi-search across multiple indexes (not used here)
    - `mcp_odoo-test_get_document_file(path)`: fetch ground-truth source text for an item (not used here)

Notes:
- The list tools also surface per-index metadata such as `uid`, `primaryKey`, `createdAt`, `updatedAt`, and optional `destination.description` and `collections[]`.
- Collections entries include `name` and often a `description` when provided by the source configuration.

---

### Indexes and Collections (mcp_all-test)

1) uid: `nitro`
   - primaryKey: `id`
   - createdAt: 2026-01-20T04:13:39.413587837Z
   - updatedAt: 2026-01-20T04:16:59.037803261Z
   - destination.description: "Nuxt Nitro documentation"
   - collections:
     - name: `nuxt` — description: "Nuxt"
   - sample search: query="introduction" → hits: 2 (e.g., docs index page, AWS provider page excerpt)

2) uid: `nuxt`
   - primaryKey: `id`
   - createdAt: 2026-01-20T04:16:59.289201049Z
   - updatedAt: 2026-01-21T12:40:20.743979253Z
   - destination.description: "Nuxt Documentation"
   - collections:
     - name: `nuxt` — description: "Nuxt"
   - sample search: query="introduction" → hits: 2 (e.g., Getting Started → Introduction)

3) uid: `nuxt-auth-utils`
   - primaryKey: `id`
   - createdAt: 2026-01-20T02:52:49.913568221Z
   - updatedAt: 2026-01-20T02:54:01.009057715Z
   - destination.description: (none provided)
   - collections: (none listed)
   - sample search: query="introduction" → hits: 0

4) uid: `nuxt-content`
   - primaryKey: `id`
   - createdAt: 2026-01-20T18:15:58.133269536Z
   - updatedAt: 2026-01-20T18:16:15.00112207Z
   - destination.description: (none provided)
   - collections:
     - name: `nuxt` — description: "Nuxt"
   - sample search: query="introduction" → hits: 2 (e.g., Nuxt Content v3 Introduction)

5) uid: `nuxt-ui`
   - primaryKey: `id`
   - createdAt: 2026-01-20T04:16:07.393662578Z
   - updatedAt: 2026-01-21T12:40:20.051591774Z
   - destination.description: (none provided)
   - collections:
     - name: `nuxt` — description: "Nuxt"
   - sample search: query="introduction" → hits: 2 (e.g., Typography → Introduction; Getting Started → Introduction)

6) uid: `odoo`
   - primaryKey: `id`
   - createdAt: 2026-01-20T04:18:43.151311632Z
   - updatedAt: 2026-01-20T16:40:41.308168184Z
   - destination.description: (none provided)
   - collections: (none listed)
   - sample search: query="introduction" → hits: 2 (e.g., redirects listings containing "introduction")

7) uid: `odoo-enterprise`
   - primaryKey: `id`
   - createdAt: 2026-01-21T12:40:21.230448994Z
   - updatedAt: 2026-01-21T12:40:22.53062429Z
   - destination.description: (none provided)
   - collections: (none listed)
   - sample search: query="introduction" → hits: 0

8) uid: `owl`
   - primaryKey: `id`
   - createdAt: 2026-01-20T18:52:30.640326117Z
   - updatedAt: 2026-01-20T18:52:42.529082273Z
   - destination.description: (none provided here)
   - collections:
     - name: `odoo` — description: "Odoo"
   - sample search: query="introduction" → hits: 0

9) uid: `readme-examples`
   - primaryKey: `id`
   - createdAt: 2026-01-20T18:15:57.06389442Z
   - updatedAt: 2026-01-20T18:15:57.627782266Z
   - destination.description: (none provided)
   - collections:
     - name: `examples` — description: "Personal examples of collections"
   - sample search: query="introduction" → hits: 0

10) uid: `readme_examples`
    - primaryKey: `id`
    - createdAt: 2026-01-20T04:18:42.118039357Z
    - updatedAt: 2026-01-20T04:18:42.905249649Z
    - destination.description: (none provided)
    - collections: (none listed)
    - sample search: query="introduction" → hits: 0

---

### Indexes and Collections (mcp_odoo-test)

1) uid: `owl`
   - primaryKey: `id`
   - createdAt: 2026-01-20T18:52:30.640326117Z
   - updatedAt: 2026-01-20T18:52:42.529082273Z
   - destination.description: (not provided in this server’s listing)
   - collections:
     - name: `odoo` — description: "Odoo"
   - sample search: query="introduction" → hits: 0

---

### Collections Summary and Descriptions

- Collection name: `nuxt`
  - Observed in indexes: `nitro`, `nuxt`, `nuxt-content`, `nuxt-ui`
  - Description provided: Yes — typically "Nuxt"
  - Interpretation: Documents related to the Nuxt ecosystem (framework, modules, UI, content)

- Collection name: `odoo`
  - Observed in indexes: `owl` (both servers)
  - Description provided: Yes — "Odoo"
  - Interpretation: Documents related to Odoo/OWL (Odoo Web Library) ecosystem

- Collection name: `examples`
  - Observed in indexes: `readme-examples`
  - Description provided: Yes — "Personal examples of collections"
  - Interpretation: A small example set illustrating how collections metadata can be declared

- No collections listed
  - Observed in indexes: `nuxt-auth-utils`, `odoo`, `odoo-enterprise`, `readme_examples`
  - Interpretation: Either collections were not configured for these indexes or aggregation at this server does not expose them for these sources

Notes on descriptions:
- When provided, each collection entry includes a succinct `description` field. Several indexes also include a `destination.description` that broadly characterizes the documentation set (e.g., "Nuxt Documentation").
- Some indexes omit both destination and collection descriptions; this is acceptable and simply indicates absent metadata in the source configuration.

---

### Sample Search Methodology

- Query used: `"introduction"`
- Limit: 2 documents per index
- Purpose: Verify index accessibility and provide a quick content sanity check without heavy traffic.

Results varied by index; Nuxt-related indexes consistently returned canonical introduction pages, while several other indexes returned zero hits for this particular query, which may be due to vocabulary differences or limited content scope.

---

### Conclusions

- Both MCP servers are reachable and list indexes successfully.
- Collections metadata is present for several indexes and typically includes brief descriptions.
- Destination-level descriptions are sometimes present (notably for `nitro` and `nuxt`).
- Sample searches confirm content is loaded for key documentation sets.
