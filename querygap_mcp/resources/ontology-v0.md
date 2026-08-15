# QueryGaP ontology v0

QueryGaP indexes public scientific documentation metadata. It does not contain
participant-level data.

## dbGaP entities and identity

- **Study**: identified by a full dbGaP accession such as
  `phs000007.v35.p16`. The `phs000007` prefix is the base study identity;
  versions and participant sets remain distinct.
- **Dataset**: identified by a `pht...v...` accession and linked to one study.
- **Variable**: identified by a `phv...v...` accession and linked to one
  dataset and study.
- **Variable report**: optional descriptive statistics and coded-value metadata
  associated with a variable and study.
- **Document**: study-scoped documentation metadata with a title, type, and
  source URL. QueryGaP currently indexes document titles, not document bodies.

The broader ingestion schema can retain supplemental data-dictionary entries,
but the hosted MCP v0 catalog does not include or expose them. They are not a
public MCP entity in this contract.

Substudies, consent groups, diseases, and molecular-data summaries are retained
inside study metadata. Some of those relationships are not normalized database
edges.

Some catalog entities are keyed to a full accession while descriptive study
metadata is stored on a base-study row. QueryGaP verifies the exact catalog
accession before using that fallback and reports `metadata_scope: base`; it does
not claim the base metadata row is version-specific.

## UK Biobank entities and identity

UK Biobank is a separate, field-centric source rather than a dbGaP study.

- **Field**: identified by an integer field ID.
- **Instance and array coordinates**: dimensions/aliases of a field, not
  independent fields.
- **Category**: fields may have multiple source parents. QueryGaP stores the
  graph during normalization but the searchable field record displays one
  deterministic category path.
- **Encoding**: optional coded-value metadata linked to a field.
- **Instance summary**: optional participant and descriptive-statistic metadata
  for a field instance.

## Normalization and ambiguity

- Dataset participant suffixes are removed when producing the normalized
  `pht...v...` identity.
- A dbGaP name or acronym can match multiple accessions. Resolution returns a
  ranked recommendation and alternatives; it is not an authoritative assertion
  that the first result is the user's intended study.
- Study ranking prefers strong name/metadata matches and then greater variable
  and dataset coverage. Full versioned accessions are preserved.
- UK Biobank alias expansion is bounded, so some middle instance/array
  combinations may not be indexed as exact aliases.

## Provenance status

Every MCP record returns a canonical source locator where available. The
current database does not consistently retain an ingestion snapshot ID,
retrieval timestamp, parser version, or source checksum for dbGaP records.
UK Biobank downloads had checksum sidecars during ingestion, but that lineage is
not retained in the searchable database. Responses disclose these limitations
rather than inventing provenance.
