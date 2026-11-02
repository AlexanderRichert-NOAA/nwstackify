# nwstackify

`nwstackify` installs the environment described by `./spack.yaml` or 
`./spack/spack.yaml`. Optional commented
`spack-url` and `spack-ref` values select the Spack checkout; otherwise it uses
the upstream Spack repository's `develop` branch.

Run `nwstackify` from the directory containing the manifest. The program prompts
for an installation root, then uses that same path for the Spack install
tree and the environment's symlink view. The temporary Spack checkout is
by default removed when the command exits.
