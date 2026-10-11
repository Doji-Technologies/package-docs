# package-docs

Builds the [docfx](https://dotnet.github.io/docfx/) documentation of every Doji package listed in
`packages.txt` and publishes it to the `gh-pages` branch, which GitHub Pages serves as
`docs.doji-tech.com/<package name>/`.

## When documentation is published

- **Once a day**, the workflow clones the latest commit of each package repository and compares the
  `version` in its `package.json` with the published one (`versions.json` in `gh-pages`). Only packages
  with a different version are rebuilt. When nothing changed, the run ends after a few seconds.
- **A push to `master` of this repository or a manual run** of the workflow rebuilds all packages. Use
  a manual run to publish documentation changes without a version bump.
- **A pull request** builds all packages and uploads them as the `docs` artifact. Nothing is published.

Only the folders of the packages that were built are replaced in `gh-pages`. A package that fails to
build keeps its published documentation, and the workflow run is marked as failed.

## How a package is built

`scripts/plan.sh` selects the packages, `scripts/build-docs.sh` builds each of them with the steps
below, and `scripts/publish.sh` copies the results into the site.

1. `scripts/prepare_docfx.py` reads the package's `docs/docfx.json` and `package.json` and writes
   `docs/docfx.generated.json`. The package repository itself is not changed. The generated config adds:
   - **Unity Editor assemblies** as `references` (`UnityEngine.*`, `UnityEditor.*`).
     `scripts/fetch-unity-editor-refs.sh` copies them out of the
     [GameCI](https://game.ci/docs/docker/docker-images) Editor image for the package's Unity version
     (`"unity"` in `package.json`, default `6000.0`). The Editor is not started, so no license is used.
   - **Dependency packages** as `references`. Every registry dependency in `package.json` is downloaded
     from the Unity registry (transitively) and its runtime assemblies are compiled to DLLs with
     `dotnet build`. Precompiled DLLs in the packages are referenced directly.
   - `AllowUnsafeBlocks` and the Unity scripting defines as `properties`.
   - **Unity's xref maps** as `xref`, so Unity types link to the Scripting Reference
     (`docs.unity3d.com/<version>/Documentation/ScriptReference/`) and to the package documentation
     (`docs.unity3d.com/Packages/<name>@<major.minor>/`).
2. `docfx docs/docfx.generated.json`
3. `docs/_site` is copied to `build/<package name>`.

Each step only warns when it cannot do its work. Without references the documentation still builds,
with unresolved Unity types.

Everything that is downloaded or compiled is stored in `.unity-refs/` and cached between workflow
runs. Unity's assemblies are never committed and never part of the published site.

## Run it locally

Needs bash, Python 3, curl, the .NET SDK and docfx 2.78 or newer. Use a local Unity installation
instead of Docker, and pass a local checkout of the package repository:

```bash
UNITY_EDITOR_MANAGED="C:/Program Files/Unity/Hub/Editor/6000.0.60f1/Editor/Data/Managed"   bash scripts/build-docs.sh ../com.doji.midas
```

Without arguments, the script clones and builds everything in `packages.txt`. The result is in
`build/<package name>/`.

## Add a package

Add a line to `packages.txt`: `<repository url> [branch]`. Without a branch, the default branch of
the repository is used. The site folder is the `name` in the package's `package.json`. Private
repositories must be readable with the `PAT_TOKEN` secret.

`redirects.txt` lists old site folders that redirect to a new one.
