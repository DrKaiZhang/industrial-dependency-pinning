# Dependency Surface and Version Pinning in Open-Source Industrial Software

Artefact for the JSEP submission: 1,042 dependency declarations from 19 open-source industrial and reference repositories across nine manifest ecosystems.

**Author:** Zhang Kai (张凯), Guangdong Communication Polytechnic
ORCID: 0009-0001-6564-6588

## Repository layout

```
scripts/   analysis and figure scripts (plain Python; no GPU required)
data/      inputs and authoritative result files (JSON/CSV) - every number in
           the paper regenerates from these
figures/   submission figures (PNG)
```

## Reproducing

1. Python 3.11+ (standard library only for most scripts; see script headers).
2. Run the scripts in `scripts/` in the order documented in each script header.
3. Quoted statistics are stored in the authoritative JSON files under `data/`;
   the manuscript cites these files directly.

## Data provenance

All corpora are public open-source repositories; no private or proprietary
code was used. Generation/model runs for the LLM-related artefacts were
executed on CPU with fixed seeds; parameters and seeds are recorded in
`data/models.json` (or the equivalent config file in `data/`).

## Citation

If you use this artefact, please cite the accompanying manuscript:

> Zhang Kai. Dependency Surface and Version Pinning in Open-Source Industrial Software (under review).

The release (v1.0.0) is permanently archived on Zenodo: https://doi.org/10.5281/zenodo.23444418.

## License

MIT (see `LICENSE`).
