# nwstackify

`nwstackify` installs the environment described by `./spack.yaml` or 
`./spack/spack.yaml`. Optional commented
`spack-url` and `spack-ref` values select the Spack checkout; otherwise it uses
the upstream Spack repository's `develop` branch.

Run `nwstackify` from the directory containing the manifest. The program prompts
for an installation root (with filesystem tab completion where Python's
`readline` module is available), then uses that same path for the Spack install
tree and the environment's symlink view. The temporary Spack checkout is
removed when the command exits.

System detection uses a system-to-hostname-pattern mapping.

From this project directory, install the CLI with `python -m pip install .`.
Run the test suite with `PYTHONPATH=src python -m unittest discover -s tests`.
