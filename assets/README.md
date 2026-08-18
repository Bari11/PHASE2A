# Environment assets

- `environment.glb` — static scenery: tree-trunk seat, grass, flowers.
  Sky/clouds/lake/hills/mountains are a baked background image inside
  this file, not separate meshes. No animations.
- `boy.glb`, `girl.glb` — rigged characters. Each contains 1 idle clip
  + 7 exercise clips (Head_Tilt, Head_Nod, Head_Rotation, Head_Look,
  Arm_Raise, Leg_Raise, Heel_Raise). Clip naming differs slightly
  between the two rigs (e.g. "Idle_Pose_Girl" vs "Idle_Pose_Boy") —
  ui/unplug/viewer.html's CharacterLoader resolves clip names by
  case-insensitive pattern match rather than assuming an exact string,
  so this doesn't need to be fixed in Blender.

Note: the girl rig also has a stray extra clip named "Action" not
present on the boy rig — looks like a leftover/unused NLA export
artifact. It's harmless (unreferenced), but worth checking in Blender
if you want the export perfectly clean.

Textures were downscaled from the original 4096×4096 / 2048×2048
source exports to 1024×1024 (character files went from ~53MB to ~7MB
each) — full resolution wasn't earning anything given the fixed,
non-close-up camera, and the original size was a real load-time /
VRAM concern on integrated GPUs. If you ever need to re-export from
Blender, keeping texture output at ≤1024px per map will avoid
needing to redo this step.
