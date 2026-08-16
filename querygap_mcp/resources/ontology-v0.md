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

## All of Us entities and identity

All of Us is a separate public-metadata vertical. QueryGaP does not connect to
participant-level Workbench data.

- **Search result**: identified by an opaque `aou.doc...` result ID. Use that
  ID with `get_aou_item`; do not infer an OMOP concept ID from it.
- **Variable**: a result with `is_variable: true`. This includes EHR concepts,
  grouped survey variables, program physical measurements, and Fitbit metrics.
  EHR concepts are variable-like data elements in the OMOP row model, even
  though they are not stored as one physical database column per concept.
- **EHR concept variable**: an OMOP concept with a vocabulary and concept code.
  Its `ehr_variable_role` distinguishes standard, classification, and source
  concepts. Mappings and relationships connect source codes to harmonized
  concepts without collapsing their identities.
- **Survey variable**: a grouped question identity. Versioned question
  occurrences, answer options, and scale memberships are related metadata.
- **Navigation/support record**: an instrument, module, scale, answer option,
  domain, or documentation record that helps locate or interpret variables but
  is explicitly returned with `is_variable: false`.

AoU search can filter variables by `ehr`, `survey`, `physical_measurement`, or
`fitbit`. EHR searches can additionally constrain the OMOP domain (`Condition`,
`Drug`, `Measurement`, or `Procedure`), EHR role (`standard`, `source`, or
`classification`), and vocabulary such as LOINC, SNOMED, RxNorm, ICD10CM, NDC,
or CPT4. EHR-specific filters require the explicit `ehr` variable type.

AoU identifiers can include OMOP concept IDs, LOINC, SNOMED CT, PPI, Fitbit,
ICD, CPT, HCPCS, RxNorm, and NDC codes. One variable can therefore have several
identifiers and links; these are identifiers for the same searchable record or
related mappings, not automatically four independent variables.

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
- AoU search spans an explicitly selected active snapshot. Result IDs from an
  inactive snapshot are not hydrated as current results.

## Provenance status

Every MCP record returns a canonical source locator where available. The
current database does not consistently retain an ingestion snapshot ID,
retrieval timestamp, parser version, or source checksum for dbGaP records.
UK Biobank downloads had checksum sidecars during ingestion, but that lineage is
not retained in the searchable database. Responses disclose these limitations
rather than inventing provenance.

AoU detail responses retain snapshot, source-locator, checksum, identifier,
link, and relationship metadata where the public source supplied it. A source
link documents or locates an element; it does not grant Workbench access.
