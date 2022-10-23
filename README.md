# scw-registry-cleaner
Clean old docker images from scaleway registry

## Usage

```yaml
  clean-old-tags:
    runs-on: ubuntu-latest
    steps:
      - name: Clean old tags
        uses: gazorby/scw-registry-cleaner@v0.1.0
        env:
          SCW_SECRET_KEY: ${{ secrets.SCW_SECRET_KEY }}
        with:
          args: >-
            --namespace=app
            # Ensure there is at least 5 remaining tags from selected ones after deletion
            --keep=5
            # Delete any selected tags older than 72hr
            --grace=72hr
            # Only match tags with the following pattern
            --pattern='^main-[a-fA-F0-9]+-(?P<ts>[1-9][0-9]*)'
```
