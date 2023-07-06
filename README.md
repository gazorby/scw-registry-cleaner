# scw-registry-cleaner
Clean old docker images from Scaleway registry

## Usage

```yaml
  clean-old-tags:
    runs-on: ubuntu-latest
    steps:
      - name: Clean old tags
        uses: gazorby/scw-registry-cleaner@v0.4.0
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

The action will print a summary of deleted image tags:

```console
+------------------------------+---------------------------------+
| Age                          | Image ref                       |
+------------------------------+---------------------------------+
| foo image                    |                                 |
+------------------------------+---------------------------------+
| 255 days, 5 hours            | app/foo:main-0dd8ec2-1666551916 |
| 254 days, 2 hours            | app/foo:main-dda1dff-1666647511 |
| 254 days, 2 hours            | app/foo:main-b01899c-1666647561 |
+------------------------------+---------------------------------+
| bar image                    |                                 |
+------------------------------+---------------------------------+
| 255 days, 5 hours            | app/bar:main-0dd8ec2-1666551920 |
| 254 days, 2 hours            | app/bar:main-dda1dff-1666647516 |
| 254 days, 2 hours            | app/bar:main-b01899c-1666647565 |
+------------------------------+---------------------------------+
```
