# Changelog — UE Apartment Placer

## Working Features (VERIFIED)

### Camera Component Creation (generator.py)
- **Cam #1**: Reuses existing `PorchPawnArrow` component from blueprint
- **Cam #2+**: Created via `AddComponentByClass` with `BalconyViewArrowComponent`
- **Positioning**: `set_world_location` + `set_world_rotation` on strongly-typed ref
- **Visibility**: `set_visibility(False)` + `set_hidden_in_game(True)` hides red arrows
- **Yaw**: Negated in `_fmt_apt_type_info` to correct Y-axis inversion
- **Simplex recognition**: All cameras recognized by Simplex Entity Configurator

### Camera Transform Sync (plan_canvas.py)
- **Move**: Cameras translate with polygon during drag, world coords recalculated on release
- **Transform (rotate/scale)**: Camera img_x/img_y and yaw_deg updated during drag
- **Vertex edit**: Camera world coords recalculated on vertex drag, add, and delete
- **Commit button**: Explicit recalculate of camera world_x_m, world_y_m, z_cm from polygon state
- **Visual indicator**: Committed (checkmark) vs uncommitted (asterisk) on polygon labels

## Known Issues (NOT YET FIXED)
- Moving the actor in Unreal Editor causes Simplex to lose additional cameras
  (only first PorchPawnArrow survives). Root cause unknown — needs investigation
  in UE editor, NOT speculative code changes.

## Reverted Changes (BROKE THINGS)
- `set_editor_property('CreationMethod', INSTANCE)` — caused cameras at 0,0,0
- `k2_attach_to_component(root, KEEP_RELATIVE)` — caused cameras at 0,0,0
- `set_editor_property('RelativeLocation', ...)` — caused inconsistent placement
- `set_relative_location` for existing PorchPawnArrow — caused wrong positions
- Using `call_method` return value directly — weakly typed, methods silently fail
