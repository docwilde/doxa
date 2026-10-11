# Release publication

DOXA's current public release is a Rust beta. The version keeps its `beta.N`
suffix and the README keeps the beta warning. Publish the current recommended
release with GitHub's prerelease flag disabled and explicitly select Latest:

```sh
gh release create TAG --repo docwilde/doxa --verify-tag \
  --title TITLE --notes-file NOTES --prerelease=false --latest
```

GitHub excludes prereleases from `/releases/latest`. Marking every beta as a
GitHub prerelease left that endpoint advertising beta.20 after beta.43 shipped.
Use the prerelease flag only for an explicitly separate preview channel.

After publication, verify the advertised tag and the public redirect:

```sh
gh api repos/docwilde/doxa/releases/latest \
  --jq '{tag_name,prerelease,draft,html_url}'
curl -s -o /dev/null -w '%{url_effective}\n' -L \
  https://github.com/docwilde/doxa/releases/latest
```

The advertised tag must match the shipped version. Continue to use protected
PR merges, required CI checks, annotated tags, and notes from the CHANGELOG.
Verify that the local and remote tag resolve to the merged commit on main.

See [GitHub's release API](https://docs.github.com/en/rest/releases/releases)
for the Latest and prerelease rules.
