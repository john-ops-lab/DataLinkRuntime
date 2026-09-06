# DLR builtin dependency source

The Control-owned library persists Python wheels, npm tgz archives and Maven JAR/POM/metadata materials. It does not mirror public registries or fetch missing dependencies.

## Deployment and workflow

Back up the deployment, run `alembic upgrade head`, then upgrade Control and Workers. Three builtin sources are seeded without changing existing defaults. Builtin tasks require the Worker capability `builtin_packages_v1`; external-source tasks remain compatible with older Workers.

Compose mounts `dlr_builtin_packages` at `/var/lib/dlr/builtin-packages` on Control. Standalone deployments set `DLR_BUILTIN_PACKAGE_ROOT` to a persistent local directory. All Control processes must share the filesystem, supporting POSIX flock, atomic rename and fsync.

Open **System Settings → Dependency sources → Builtin library**, upload materials, and explicitly select the builtin default for each language. Maven folder uploads preserve paths below the selected repository root; individual uploads require the group/artifact/version directory prefix. External sources remain available in source configuration.

Select a saved Adapter and a Worker to check installation. This creates an ordinary queued Attempt with resource limits, but executes no business script and injects no business credentials. Java compiles the saved code, so source compilation errors can also fail the check. Results distinguish uploaded files, missing dependencies, environment incompatibility and other failures. Detailed package-manager logs remain in Adapter execution history.

## Prepare offline materials

Use the same OS, architecture and runtime as the target Worker; the current image uses Python 3.13, Node.js 22 and Java 21.

- Python: prepare the complete wheel dependency closure, for example `pip download --only-binary=:all: -r requirements.txt -d wheelhouse`. Source builds are unsupported. Match ABI/platform tags and Requires-Python. Index options, local paths and direct URL dependencies are rejected, including URLs inside wheel metadata.
- npm: use `npm pack` for every package in the actual dependency closure, including required optional/peer dependencies. Registry version ranges are resolved by npm against a temporary loopback registry. Direct URLs, Git/local references, bundled node_modules and embedded npm-shrinkwrap are rejected. Installation uses `--ignore-scripts`; packages requiring postinstall builds/downloads require compatible prebuilt materials and an actual installation check.
- Maven: with an equivalent dependency POM and empty local repository, run `mvn -Dmaven.repo.local=/path/to/repository org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies -DoutputDirectory=/path/to/deps`. Upload JARs, POMs and necessary metadata XML, including parents, BOMs, transitives, dependency-plugin 3.8.1 and its dependencies. Ignore `.lastUpdated`, `_remote.repositories` and checksum sidecars. SDK JARs alone are insufficient. POM coordinates must be concrete and verifiable against the repository path.

Worker uses uv with an offline wheelhouse, npm with only the Attempt loopback registry, and Maven with `-o`, isolated settings and a material repository. Control Claim-authenticated downloads verify length and SHA-256 before installation. Package managers receive no administrator or Worker token. Accepted builtin tasks freeze the category material list; cache identity includes that snapshot and runtime identity, allowing verified warm reuse without downloads.

## Capacity and deletion

The default aggregate limit is **1 GiB (1,073,741,824 bytes)** across all languages. Administrators can change it. Saved bytes, concurrent upload reservations and `DLR_BUILTIN_PACKAGE_MIN_FREE_BYTES` disk headroom (default 256 MiB) are checked. Worker caches and extracted environments use separate Worker limits.

Interrupted uploads retain their reservation until explicitly released. Release requires an idle file lock and successful removal of temporary/orphan bodies. There is no automatic package eviction or attachment expiry. Empty lock files may remain without package content.

Deletion requires confirmation and blocks active downloads, unfinished tasks and unconfirmed installation cleanup. Resolve uncertain cleanup through Worker recovery; do not clear holds by editing database rows. Success follows physical unlink and directory fsync. Failed deletion stays visible and retryable; the download entry disappears after deletion, with no retained content copy.

Deleting source materials does not uninstall Worker environments or revoke a package. A new Worker or a reclaimed cache may be unable to rebuild. Changes to the material collection change new-task cache identity and may trigger rebuilding.

## Backup and restore

Back up PostgreSQL and the complete builtin directory together, including `.blob`, `.part` and lock files. Stop new tasks and library writes, drain active tasks/uploads/downloads, then stop Control and Workers before taking a consistent snapshot. Restore database and files from the same point, preserving ownership, permissions and persistent mounts.

Verify sizes, SHA-256 identities and reservation/body correspondence; run installation checks on each target environment after restart. Never combine different backup times or restore metadata alone. Uncertain reservations continue counting toward capacity; releasing them removes their temporary/orphan bodies. Do not remove files manually while services are running.
