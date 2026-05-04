# Selwyn District Council

Support for schedules provided by [Selwyn District Council](https://www.selwyn.govt.nz/services/rubbish-recycling-And-organics/kerbside-collections), New Zealand.

## Configuration via configuration.yaml

```yaml
waste_collection_schedule:
  sources:
    - name: selwyn_govt_nz
      args:
        address: STREET_NUMBER_AND_STREET_NAME
```

### Configuration Variables

**address**
*(string) (required)*

The full kerbside address as it appears on the council lookup. Partial addresses also work but may match multiple properties; in that case an ambiguity error lists the candidates.

## Example

```yaml
waste_collection_schedule:
  sources:
    - name: selwyn_govt_nz
      args:
        address: 15 Meijer Drive Lincoln
```

## How to get the source arguments

Visit the [Selwyn District Council collection-day lookup](https://www.selwyn.govt.nz/services/rubbish-recycling-And-organics/kerbside-collections/collection-days-and-routes), type your address into the search box, and copy the full address shown in the autocomplete suggestions.
