#!/usr/bin/env python3
"""Prepare a package repository for a docfx build that resolves Unity types.

Reads <repo>/docs/docfx.json and writes <repo>/docs/docfx.generated.json with:
  * "references": Unity Editor assemblies, precompiled package DLLs, and
    reference DLLs compiled from the package's UPM dependencies
  * "properties": AllowUnsafeBlocks and Unity scripting defines
  * build "xref": Unity's xref maps, rewritten to absolute docs.unity3d.com URLs

Nothing in the package repository is modified. Everything that is downloaded or
compiled goes to the refs directory (default: <package-docs>/.unity-refs), which
is safe to cache between runs.

Every step degrades to a warning: without Editor assemblies, network access or
the .NET SDK, the generated config still builds, just with unresolved types.

Usage:
  prepare_docfx.py <repo>                  print the path of the generated config
  prepare_docfx.py <repo> --print-unity    print the Unity version (major.minor)
  prepare_docfx.py <repo> --print-name     print the package name
"""

import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

REGISTRY = "https://download.packages.unity.com"
DOCS = "https://docs.unity3d.com"
DEFAULT_UNITY = "6000.0"

# Dependencies that are never part of a package's public API.
SKIP_PACKAGES = re.compile(
    r"^com\.unity\.(modules\..*|test-framework.*|ext\.nunit|nuget\.mono-cecil|ide\..*)$")
# Assemblies inside a dependency that are not needed to resolve its public API.
SKIP_ASSEMBLY = re.compile(r"(Tests?(\.|$)|CodeGen|DocCodeSamples|Benchmarks?)", re.I)
# Unity ignores folders ending in "~" and starting with "."; tests never matter here.
SKIP_DIR = re.compile(r"(^\.|~$|Tests?(\.|$)|^Samples$)", re.I)

# (major, last minor) of every Unity release stream, for UNITY_x_y_OR_NEWER.
UNITY_STREAMS = [(5, 6), (2017, 4), (2018, 4), (2019, 4), (2020, 3), (2021, 3), (2022, 3), (2023, 2)]
BASE_DEFINES = [
    "UNITY_EDITOR", "UNITY_EDITOR_64", "UNITY_STANDALONE", "UNITY_64", "UNITY_ASSERTIONS",
    "ENABLE_UNITY_COLLECTIONS_CHECKS", "NET_STANDARD", "NET_STANDARD_2_0", "NET_STANDARD_2_1",
    "CSHARP_7_3_OR_NEWER",
]


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def info(message):
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- json

def load_json(path):
    """Loads JSON the way docfx (Newtonsoft) does: comments and trailing commas are fine."""
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        elif c == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


# ------------------------------------------------------------------------ versions

def version_tuple(version):
    return tuple(int(x) for x in re.findall(r"\d+", version.split("-")[0])[:3])


def major_minor(version):
    return ".".join(version.split(".")[:2])


def unity_defines(unity):
    major, minor = (int(x) for x in unity.split(".")[:2])
    streams = UNITY_STREAMS + [(6000, 99)]
    defines = []
    for stream_major, last_minor in streams:
        first_minor = 3 if stream_major == 5 else 0 if stream_major >= 6000 else 1
        for m in range(first_minor, last_minor + 1):
            if (stream_major, m) <= (major, minor):
                defines.append(f"UNITY_{stream_major}_{m}_OR_NEWER")
    defines += [f"UNITY_{major}", f"UNITY_{major}_{minor}"]
    return defines


def version_defines(asmdef, unity, packages):
    """Evaluates asmdef versionDefines. Only plain 'x.y.z' (= at least) expressions."""
    defines = []
    for entry in asmdef.get("versionDefines") or []:
        name, expression = entry.get("name", ""), entry.get("expression", "")
        have = f"{unity}.999" if name == "Unity" else packages.get(name)
        if not have or not entry.get("define"):
            continue
        if not expression or (re.match(r"^\d[\w.\-]*$", expression)
                              and version_tuple(have) >= version_tuple(expression)):
            defines.append(entry["define"])
    return defines


# ------------------------------------------------------------------------ assemblies

def is_managed_dll(path):
    """True if the file is a PE image with a CLI header (a .NET assembly)."""
    try:
        with open(path, "rb") as f:
            data = f.read(4096)
        pe = int.from_bytes(data[0x3C:0x40], "little")
        if data[:2] != b"MZ" or data[pe:pe + 4] != b"PE\0\0":
            return False
        optional = pe + 24
        magic = int.from_bytes(data[optional:optional + 2], "little")
        directories = optional + (112 if magic == 0x20B else 96)
        clr = directories + 14 * 8
        return int.from_bytes(data[clr:clr + 4], "little") != 0
    except (OSError, IndexError):
        return False


def walk(root):
    """Yields files below root, skipping folders Unity ignores and test folders."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not SKIP_DIR.search(d))
        for name in sorted(files):
            yield os.path.join(folder, name)


def precompiled_dlls(root):
    """Managed plugin DLLs of a package, one per file name (shallowest path wins)."""
    found = {}
    for path in walk(root):
        if path.lower().endswith(".dll") and os.path.exists(path + ".meta") and is_managed_dll(path):
            name = os.path.basename(path).lower()
            if name not in found or path.count(os.sep) < found[name].count(os.sep):
                found[name] = path
    return sorted(found.values())


def editor_references(editor_dir):
    """Unity Editor assemblies: the module assemblies plus the UnityEngine/UnityEditor facades."""
    if not editor_dir or not os.path.isdir(editor_dir):
        return []
    modules = os.path.join(editor_dir, "UnityEngine")
    folder = modules if os.path.isdir(modules) else editor_dir
    return sorted(p for p in glob.glob(os.path.join(folder, "*.dll")) if is_managed_dll(p))


# -------------------------------------------------------------------------- packages

def open_url(url):
    """Returns (stream, close). Uses curl when present: it follows the system certificate store."""
    curl = shutil.which("curl")
    if curl:
        process = subprocess.Popen([curl, "-fsSL", "--retry", "3", url], stdout=subprocess.PIPE)

        def close():
            while process.stdout.read(1 << 16):
                pass
            process.stdout.close()
            if process.wait() != 0:
                raise OSError(f"curl exit code {process.returncode}")
        return process.stdout, close
    request = urllib.request.Request(url, headers={"User-Agent": "doji-package-docs"})
    response = urllib.request.urlopen(request, timeout=120)
    return response, response.close


def download(url):
    stream, close = open_url(url)
    data = stream.read()
    close()
    return data


def unpack_package(stream, dest):
    """Unpacks the files of a package tarball that matter for compiling it."""
    with tarfile.open(fileobj=stream, mode="r|gz") as tar:
        for member in tar:
            parts = member.name.split("/")[1:]  # drop the leading "package/"
            if not member.isfile() or not parts or ".." in parts:
                continue
            if any(SKIP_DIR.search(p) for p in parts[:-1]):
                continue
            if not parts[-1].lower().endswith((".cs", ".dll", ".json", ".asmdef", ".dll.meta", ".asmdef.meta")):
                continue
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(tar.extractfile(member), out)


def fetch_package(name, version, refs):
    """Downloads and unpacks a package tarball from the Unity registry. Returns its folder."""
    dest = os.path.join(refs, "packages", f"{name}@{version}")
    if os.path.exists(os.path.join(dest, "package.json")):
        return dest
    url = f"{REGISTRY}/{name}/-/{name}-{version}.tgz"
    info(f"  fetching {name}@{version}")
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        stream, close = open_url(url)
        unpack_package(stream, tmp)
        close()
    except (urllib.error.URLError, tarfile.TarError, OSError) as e:
        warn(f"could not fetch {name}@{version} from the Unity registry ({e}); its types stay unresolved")
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    os.replace(tmp, dest)
    return dest


def resolve_dependencies(manifest, refs):
    """Returns {name: (version, folder)} for all transitive registry dependencies."""
    resolved = {}
    todo = list((manifest.get("dependencies") or {}).items())
    while todo:
        name, version = todo.pop(0)  # direct dependencies first
        if SKIP_PACKAGES.match(name) or not re.match(r"^\d", version):
            continue
        if name in resolved and version_tuple(resolved[name][0]) >= version_tuple(version):
            continue
        folder = fetch_package(name, version, refs)
        if not folder:
            continue
        resolved[name] = (version, folder)
        todo += list((load_json(os.path.join(folder, "package.json")).get("dependencies") or {}).items())
    return resolved


# ---------------------------------------------------------------- dependency compile

class Assembly:
    def __init__(self, path, package):
        self.path = path
        self.dir = os.path.dirname(path)
        self.package = package
        self.data = load_json(path)
        self.name = self.data.get("name") or os.path.splitext(os.path.basename(path))[0]
        self.guid = None
        try:
            with open(path + ".meta", encoding="utf-8") as f:
                match = re.search(r"^guid:\s*(\w+)", f.read(), re.M)
                self.guid = match.group(1) if match else None
        except OSError:
            pass

    def wanted(self):
        data = self.data
        if SKIP_ASSEMBLY.search(self.name):
            return False
        if "UNITY_INCLUDE_TESTS" in (data.get("defineConstraints") or []):
            return False
        if any("TestRunner" in r for r in data.get("references") or []):
            return False
        return (data.get("includePlatforms") or []) != ["Editor"]


def find_assemblies(packages):
    assemblies = []
    for name, (version, folder) in sorted(packages.items()):
        for path in walk(folder):
            if path.endswith(".asmdef"):
                try:
                    assemblies.append(Assembly(path, name))
                except (ValueError, OSError) as e:
                    warn(f"could not read {path} ({e})")
    return assemblies


def assembly_sources(assembly, all_dirs):
    """The .cs files that belong to an asmdef: below its folder, not below a nested asmdef."""
    nested = [d for d in all_dirs if d != assembly.dir and d.startswith(assembly.dir + os.sep)]
    return [p for p in walk(assembly.dir)
            if p.endswith(".cs") and not any(p.startswith(d + os.sep) for d in nested)]


def has_dotnet():
    try:
        subprocess.run(["dotnet", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def compile_assembly(assembly, sources, references, defines, out_dir, work_dir):
    """Compiles one dependency assembly to a DLL. Returns the DLL path, or None on failure."""
    dll = os.path.join(out_dir, assembly.name + ".dll")
    failed = dll + ".failed"
    if os.path.exists(dll):
        return dll
    if os.path.exists(failed):
        return None
    info(f"  compiling {assembly.name}")
    os.makedirs(out_dir, exist_ok=True)
    project_dir = os.path.join(work_dir, assembly.name)
    shutil.rmtree(project_dir, ignore_errors=True)
    os.makedirs(project_dir, exist_ok=True)
    items = "".join(f'    <Compile Include="{escape(p)}" />\n' for p in sources)
    items += "".join(
        f'    <Reference Include="{escape(os.path.splitext(os.path.basename(p))[0])}">'
        f"<HintPath>{escape(p)}</HintPath><Private>false</Private></Reference>\n" for p in references)
    project = os.path.join(project_dir, assembly.name + ".csproj")
    with open(project, "w", encoding="utf-8") as f:
        f.write(f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>netstandard2.1</TargetFramework>
    <AssemblyName>{escape(assembly.name)}</AssemblyName>
    <LangVersion>9.0</LangVersion>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <EnableDefaultItems>false</EnableDefaultItems>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <GenerateTargetFrameworkAttribute>false</GenerateTargetFrameworkAttribute>
    <ProduceReferenceAssembly>false</ProduceReferenceAssembly>
    <GenerateDependencyFile>false</GenerateDependencyFile>
    <DebugType>none</DebugType>
    <WarningLevel>0</WarningLevel>
    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>
    <NoWarn>$(NoWarn);CS0436;CS1701;CS1702;CS8632;MSB3277</NoWarn>
    <RunAnalyzers>false</RunAnalyzers>
    <DefineConstants>{escape(";".join(defines))}</DefineConstants>
    <OutDir>{escape(os.path.join(project_dir, "out") + os.sep)}</OutDir>
  </PropertyGroup>
  <ItemGroup>
{items}  </ItemGroup>
</Project>
""")
    result = subprocess.run(
        ["dotnet", "build", project, "-c", "Release", "-nologo", "-v", "quiet", "-clp:NoSummary",
         "-nodeReuse:false", "-p:UseSharedCompilation=false"],
        capture_output=True, text=True)
    built = os.path.join(project_dir, "out", assembly.name + ".dll")
    if result.returncode != 0 or not os.path.exists(built):
        errors = [l for l in result.stdout.splitlines() if ": error " in l]
        warn(f"could not compile {assembly.name} ({assembly.package}); its types stay unresolved. "
             f"{len(errors)} errors, first: {errors[0] if errors else result.stdout.strip()[-300:]}")
        open(failed, "w").close()
        return None
    shutil.copyfile(built, dll)
    shutil.rmtree(project_dir, ignore_errors=True)
    return dll


def build_dependencies(packages, engine, unity, refs):
    """Compiles the runtime assemblies of all dependency packages, in reference order."""
    versions = {name: version for name, (version, _) in packages.items()}
    plugins = [dll for _, folder in packages.values() for dll in precompiled_dlls(folder)]
    assemblies = find_assemblies(packages)
    wanted = [a for a in assemblies if a.wanted()]
    if not wanted:
        return plugins
    if not has_dotnet():
        warn("dotnet SDK not found; dependency packages are not compiled")
        return plugins
    if not engine:
        warn("no Unity Editor assemblies; dependency packages are not compiled")
        return plugins

    by_key = {}
    for a in assemblies:
        by_key[a.name] = a
        if a.guid:
            by_key["GUID:" + a.guid] = a
    all_dirs = [a.dir for a in assemblies]
    built, done = {}, set()

    def build(assembly):
        if assembly.name in done:
            return built.get(assembly.name)
        done.add(assembly.name)
        references = list(engine) + plugins
        for key in assembly.data.get("references") or []:
            dependency = by_key.get(key)
            if dependency and dependency.wanted():
                dll = build(dependency)
                if dll:
                    references.append(dll)
        sources = assembly_sources(assembly, all_dirs)
        if not sources:
            return None
        version = versions[assembly.package]
        out_dir = os.path.join(refs, "lib", unity, f"{assembly.package}@{version}")
        work_dir = os.path.join(refs, "obj", unity, f"{assembly.package}@{version}")
        defines = unity_defines(unity) + BASE_DEFINES + version_defines(assembly.data, unity, versions)
        dll = compile_assembly(assembly, sources, references, defines, out_dir, work_dir)
        if dll:
            built[assembly.name] = dll
        return dll

    for assembly in wanted:
        build(assembly)
    return plugins + sorted(built.values())


# ------------------------------------------------------------------------------ xref

def fetch_xref_map(url, base, dest):
    """Downloads a Unity xref map and rewrites its relative hrefs to absolute URLs."""
    if os.path.exists(dest):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        text = download(url).decode("utf-8-sig")
    except (urllib.error.URLError, OSError) as e:
        warn(f"no xref map at {url} ({e}); these types get no links")
        return None
    out = io.StringIO()
    for line in text.splitlines():
        match = re.match(r"^(\s+href:\s*)(['\"]?)(.*?)\2\s*$", line)
        if match and not re.match(r"^[a-z]+://", match.group(3)):
            line = f"{match.group(1)}{match.group(2)}{base}{match.group(3)}{match.group(2)}"
        else:
            # The Scripting Reference map names generic types "NativeArray_1".
            match = re.match(r"^(\s+name:\s*')(\w+?)_\d+'\s*$", line)
            if match:
                line = f"{match.group(1)}{match.group(2)}'"
        out.write(line + "\n")
    with open(dest + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(out.getvalue())
    os.replace(dest + ".tmp", dest)
    return dest


def xref_maps(unity, packages, refs):
    maps = []
    folder = os.path.join(refs, "xref")
    base = f"{DOCS}/{unity}/Documentation/ScriptReference/"
    maps.append(fetch_xref_map(base + "xrefmap.yml", base, os.path.join(folder, f"unity-{unity}.yml")))
    for name, (version, _) in sorted(packages.items()):
        short = f"{name}@{major_minor(version)}"
        base = f"{DOCS}/Packages/{short}/"
        maps.append(fetch_xref_map(base + "xrefmap.yml", base, os.path.join(folder, f"{short}.yml")))
    return [m for m in maps if m]


# ------------------------------------------------------------------------------ main

def file_mappings(paths):
    """docfx file mappings (one per folder) for a list of absolute file paths."""
    folders = {}
    for path in paths:
        folders.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
    return [{"src": folder.replace(os.sep, "/"), "files": files} for folder, files in folders.items()]


def find_package(repo, config):
    """Finds the UPM package folder: the docfx source folder that has a package.json."""
    docs = os.path.join(repo, "docs")
    candidates = []
    for entry in config.get("metadata") or []:
        for src in entry.get("src") or []:
            if isinstance(src, dict) and src.get("src"):
                candidates.append(os.path.normpath(os.path.join(docs, src["src"])))
    candidates += sorted(os.path.dirname(p) for p in glob.glob(os.path.join(repo, "*", "package.json")))
    for folder in candidates:
        manifest = os.path.join(folder, "package.json")
        if os.path.exists(manifest):
            return folder, load_json(manifest)
    return None, {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="package repository (contains docs/docfx.json)")
    parser.add_argument("--refs", default=os.environ.get("UNITY_REFS_DIR") or os.path.join(here, "..", ".unity-refs"),
                        help="cache folder for downloaded and compiled references")
    parser.add_argument("--editor-managed", default=os.environ.get("UNITY_EDITOR_MANAGED"),
                        help="Editor/Data/Managed of a Unity install (default: <refs>/editor/<unity>)")
    parser.add_argument("--default-unity", default=os.environ.get("UNITY_DEFAULT_VERSION") or DEFAULT_UNITY,
                        help="Unity version for packages without a \"unity\" field")
    parser.add_argument("--print-unity", action="store_true")
    parser.add_argument("--print-name", action="store_true")
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    refs = os.path.abspath(args.refs)
    source = os.path.join(repo, "docs", "docfx.json")
    config = load_json(source)
    package_dir, manifest = find_package(repo, config)
    unity = major_minor(manifest.get("unity") or args.default_unity)

    if args.print_unity:
        print(unity)
        return
    if args.print_name:
        print(manifest.get("name") or os.path.basename(repo))
        return

    info(f"{manifest.get('name', repo)}: Unity {unity}")
    engine = editor_references(args.editor_managed or os.path.join(refs, "editor", unity))
    if not engine:
        warn(f"no Unity {unity} Editor assemblies found; UnityEngine types stay unresolved")
    packages = resolve_dependencies(manifest, refs)
    references = engine + build_dependencies(packages, engine, unity, refs)
    if package_dir:
        references += precompiled_dlls(package_dir)

    defines = unity_defines(unity) + BASE_DEFINES
    versions = {name: version for name, (version, _) in packages.items()}
    if package_dir:
        for path in walk(package_dir):
            if path.endswith(".asmdef"):
                defines += version_defines(load_json(path), unity, versions)
    defines = list(dict.fromkeys(defines))

    for entry in config.get("metadata") or []:
        entry["references"] = list(entry.get("references") or []) + file_mappings(references)
        properties = dict(entry.get("properties") or {})
        properties.setdefault("AllowUnsafeBlocks", "true")
        existing = properties.get("DefineConstants")
        properties["DefineConstants"] = ";".join(([existing] if existing else []) + defines)
        entry["properties"] = properties

    build = config.setdefault("build", {})
    xref = build.get("xref") or []
    build["xref"] = ([xref] if isinstance(xref, str) else list(xref)) + xref_maps(unity, packages, refs)

    generated = os.path.join(repo, "docs", "docfx.generated.json")
    with open(generated, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    info(f"  {len(references)} references, {len(build['xref'])} xref maps")
    print(generated)


if __name__ == "__main__":
    main()
