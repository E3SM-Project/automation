# gpu-arch

Composite action that detects the GPU of the runner and tells you the matching
Kokkos architecture and CMake configs. Replaces the copies of this logic in
workflow files (Nvidia via `nvidia-smi`, AMD via `rocminfo`).

```yaml
steps:
  - name: Detect GPU
    id: gpu
    uses: E3SM-Project/automation/gpu-arch@v1
  - name: Configure
    run: cmake ... -D"Kokkos_ARCH_${{ steps.gpu.outputs.kokkos-arch }}=ON"
```

## Outputs

| Output | Example (Nvidia / AMD) |
| --- | --- |
| `vendor` | `nvidia` / `amd` |
| `gpu` | `NVIDIA H100 80GB HBM3` / `gfx942` |
| `kokkos-arch` | `HOPPER90` / `AMD_GFX942` |
| `cmake-configs` | `Kokkos_ARCH_HOPPER90=ON;CMAKE_CUDA_ARCHITECTURES=90` / `Kokkos_ARCH_AMD_GFX942=ON` |

`cmake-configs` is a semicolon-separated list of `key=value` pairs.

## Behavior

- The vendor is auto-detected: `nvidia-smi` if present, else `rocminfo`.
- Only the first GPU is considered; the runner should be homogeneous.
- Fails (with an error annotation) if neither tool is found, if the tool fails,
  or if the GPU is not in the supported list.

| Vendor | Supported |
| --- | --- |
| Nvidia | H100 (`HOPPER90`), A100 (`AMPERE80`), V100 (`VOLTA70`) |
| AMD | `gfx942` (`AMD_GFX942`), `gfx90a` (`AMD_GFX90A`), `gfx908` (`AMD_GFX908`) |

To support a new GPU, add a line to the table in `action.yml` and a matching row to
the `gpu-arch` matrix in `.github/workflows/test-actions.yaml`.

## Tests

GitHub-hosted runners have no GPU, so CI fakes the tools: `tests/fake-gpu-tool` is
copied to `nvidia-smi` or `rocminfo` on `PATH`, printing the GPU of each matrix row
(the `rocminfo` output has the same shape as the real one: a CPU agent first, then
the GPU), and the workflow checks the outputs, including the failure cases
(unsupported GPU, tool exits non-zero, no tool).

This checks the mapping logic, not the real output format of the tools on your GPU
machines: the first real use there is the real test.
