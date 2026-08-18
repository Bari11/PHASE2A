# Canary — Blender Exercise Export Guide
## Getting your boy.blend animations into canary_boy.glb

---

## Overview of the pipeline

```
boy.blend  →  Mixamo (auto-rig + animations)  →  back into Blender
           →  fix axes  →  export canary_boy.glb
```

Each exercise becomes one **named Action** inside the GLB.
Three.js reads those names and plays them on command.

---

## PART 1 — Prepare your model in Blender

### Step 1 — Open boy.blend

1. Open Blender.
2. File → Open → select **boy.blend**.
3. Make sure you can see the character in the viewport.

### Step 2 — Check the mesh is clean for Mixamo

Mixamo needs a single mesh with no loose parts floating around.

1. Click on the character mesh in the viewport.
2. Press **Tab** to enter Edit Mode.
3. Press **A** to select everything.
4. Mesh → Merge → By Distance  (removes duplicate vertices).
5. Press **Tab** to return to Object Mode.
6. If there are separate body parts (head, body, arms as different objects):
   - Select all mesh objects with **Shift+Click**.
   - Press **Ctrl+J** to join them into one mesh.
7. Make sure there is **no armature (skeleton)** yet — Mixamo will add one.
   If there is one: select the armature → press **X** → Delete.

### Step 3 — Export as FBX for Mixamo

1. File → Export → **FBX (.fbx)**
2. In the export panel on the right side:
   - **Selected Objects**: OFF (export everything)
   - **Apply Scalings**: FBX All
   - **Forward**: **-Z Forward**
   - **Up**: **Y Up**
   - **Apply Unit**: ON
   - **Armature**: Uncheck "Add Leaf Bones"
   - **Mesh**: Smoothing → Face
3. Name it **boy_for_mixamo.fbx**
4. Click **Export FBX**

---

## PART 2 — Rig and download animations from Mixamo

### Step 4 — Upload to Mixamo

1. Go to **https://www.mixamo.com**
2. Sign in (free Adobe account).
3. Click **Upload Character** (top right).
4. Drag **boy_for_mixamo.fbx** into the upload window.
5. Mixamo auto-rigs — it will show you the skeleton preview.
6. Adjust the chin / wrists / elbows / knees markers if needed.
7. Click **Next** → **Finish**.

### Step 5 — Download the T-pose (bind pose first)

Before downloading animations, get the rigged character in T-pose:

1. In the Animations tab, search **"T-Pose"**.
2. Select it.
3. Click **Download**:
   - Format: **FBX**
   - Skin: **With Skin**
   - Frames per second: **30**
   - Keyframe Reduction: **none**
4. Save as **boy_tpose.fbx**

### Step 6 — Download each exercise animation

For **each** of the 17 exercises, do this:

1. Search for the animation name in Mixamo (see table below).
2. Adjust the parameters (speed, character overlap) to look natural.
3. Click **Download**:
   - Format: **FBX**
   - Skin: **Without Skin**  ← important, keeps file small
   - Frames per second: **30**
   - Keyframe Reduction: **none**
4. Name the file exactly as shown in the table.

### Animation search table

| Canary exercise name         | Mixamo search term              | File name to save as              |
|------------------------------|---------------------------------|-----------------------------------|
| Chin Tucks                   | "chin tuck" / "neck retraction" | anim_chin_tuck.fbx                |
| Head Nods (up/down)          | "head nod" / "yes"              | anim_head_up.fbx                  |
| Head Tilt Left               | "head tilt" / "neck tilt"       | anim_head_tilt_left.fbx           |
| Head Tilt Right              | same as above, mirrored in Blender | anim_head_tilt_right.fbx       |
| Head Look Left               | "look left" / "head turn"       | anim_head_look_left.fbx           |
| Head Look Right              | "look right"                    | anim_head_look_right.fbx          |
| Neck Rolls                   | "neck roll" / "head circles"    | anim_head_rotation.fbx            |
| Leg Raise Left               | "seated leg raise"              | anim_leg_raise_left.fbx           |
| Leg Raise Right              | "seated leg raise" (mirror)     | anim_leg_raise_right.fbx          |
| Lateral Bend                 | "side stretch" / "lateral bend" | anim_lateral_bend.fbx             |
| Knee Raise                   | "knee raise" / "seated march"   | anim_knee_raise.fbx               |
| Shoulder Socket Rotation     | "shoulder roll" / "arm circles" | anim_shoulder_socket_rotation.fbx |
| Shoulder Drop                | "shoulder shrug"                | anim_shoulder_drop.fbx            |
| Thoracic Rotation            | "torso twist" / "trunk rotation"| anim_thoracic_rotation.fbx        |
| Arm Raises                   | "arm raise" / "lateral raise"   | anim_arm_raises.fbx               |
| Leg Extension                | "leg extension" / "kick out"    | anim_leg_extension.fbx            |
| Heel Raises                  | "calf raise" / "heel raise"     | anim_heel_raises.fbx              |

**Tip**: If Mixamo doesn't have an exact match, pick the closest and adjust
the speed slider so it looks calm and deliberate (not athletic).

---

## PART 3 — Assemble everything back in Blender

### Step 7 — Import the T-pose (rigged character)

1. In Blender, File → Import → **FBX (.fbx)**
2. Select **boy_tpose.fbx**
3. Import settings:
   - **Automatic Bone Orientation**: ON  ← very important
   - **Import Normals**: ON
4. You will now have your character with the Mixamo armature.

### Step 8 — Fix the rotation (Mixamo exports with X rotated 90°)

Mixamo exports in a coordinate system rotated 90° from Blender's default.
Fix it before importing animations:

1. Click on the **Armature** object (the skeleton, not the mesh).
2. In the properties panel (press **N**) → Item tab → Rotation:
   - Set **X = -90°**, Y = 0, Z = 0
3. Press **Ctrl+A** → **All Transforms**
   (this "bakes" the rotation so it becomes the new default — rotation reads 0,0,0)
4. Now click the **Mesh** object.
5. Same thing: Ctrl+A → All Transforms.

### Step 9 — Import each animation FBX

For each of the 17 animation files:

1. File → Import → **FBX (.fbx)**
2. Select the file, e.g. **anim_chin_tuck.fbx**
3. Import settings:
   - **Automatic Bone Orientation**: ON
   - **Import Normals**: OFF (we don't need normals from animation files)
   - **Animation**: ON
   - **Import Deformation Bones Only**: OFF
4. After import, a new armature object appears. You only need its **Action**.
5. Open the **Action Editor** (bottom of screen → change editor type → Action Editor).
6. You'll see an action called something like **"Armature|mixamo.com|Layer0"**.
7. **Rename it** by clicking the name → type the clean name e.g. **chin_tuck**.
8. Click the **shield icon** (Fake User) next to the name — this prevents Blender
   from deleting the action when you delete the temporary armature.
9. Delete the temporary armature object (the one you just imported).
10. Repeat for all 17 files.

### Step 10 — Assign actions to your main armature

1. Select your **main armature** (from boy_tpose.fbx).
2. Open the Action Editor.
3. Click the action dropdown and you'll see all your renamed actions.
4. Select each one in turn — you'll see the character pose update.
   If the pose looks wrong, see Troubleshooting below.

### Step 11 — Verify the sitting pose is set as the default

Your character's **idle / resting pose** should be the sitting pose
(since they're seated on the tree trunk in Canary).

1. Select the armature.
2. In the Action Editor, select or create an action called **"idle"**.
3. In Pose Mode (Ctrl+Tab to enter), arrange the character in a natural
   seated position:
   - UpperLegs: X = 90°
   - LowerLegs: X = -90°
   - Forearms: relaxed, Z = small angle (~15°)
4. Press **I** → **Location & Rotation** to keyframe at frame 1.
5. This becomes your idle pose.

---

## PART 4 — Export as GLB

### Step 12 — Set up the scene

1. Delete any imported temporary armatures (only keep the main one).
2. Make sure the mesh is **parented** to the armature:
   - Click mesh → Shift+Click armature → Ctrl+P → **Armature Deform with Automatic Weights**.
3. Select both the **armature** and the **mesh**.

### Step 13 — Export GLB

1. File → Export → **glTF 2.0 (.glb/.gltf)**
2. Export settings (right panel):

   **Format**: GLB (single file)

   **Include**:
   - Selected Objects: ON (only export what you selected)
   - Custom Properties: OFF
   - Cameras: OFF
   - Punctual Lights: OFF

   **Transform**:
   - Y Up: ON  ← makes Three.js happy

   **Geometry**:
   - Apply Modifiers: ON
   - UVs: ON
   - Normals: ON
   - Vertex Colors: ON (if your model uses them)
   - Materials: ON

   **Animation**:
   - Animation: ON
   - Limit to Playback Range: OFF
   - Always Sample Animations: ON
   - Group by NLA Track: OFF  ← important
   - NLA Tracks: OFF
   - **Export all actions as separate NLA strips**: this is what
     puts all your renamed actions into the GLB as separate clips.
   - Shape Keys: OFF (unless you have face blendshapes)
   - Skinning: ON

3. Name the file **canary_boy.glb**
4. Click **Export glTF 2.0**

---

## PART 5 — Verify in Three.js

### Step 14 — Quick check with gltf.report

Before dropping the file into Canary:

1. Go to **https://gltf.report**
2. Drag **canary_boy.glb** into the window.
3. Click the **Animations** tab on the left.
4. You should see all 17 animation names listed (chin_tuck, heel_raises, etc.).
5. Click each one — character should animate correctly.

If animations are listed but look wrong (T-pose, lying flat, etc.) →
see troubleshooting below.

---

## Troubleshooting

### Problem: Character lies flat / appears lying down
**Cause**: The -90° X rotation from Mixamo was not baked before export.
**Fix**: Step 8 — make sure you did Ctrl+A → All Transforms on the armature
BEFORE importing animations.

### Problem: Animations look correct in Action Editor but broken in GLB
**Cause**: Blender exported the world-space instead of local bone transforms.
**Fix**:
1. Select the armature.
2. Go to Object Properties → Custom Properties.
3. Make sure there's no leftover "rest_location" override.
4. Re-export with "Always Sample Animations: ON".

### Problem: Only one animation shows up in GLB
**Cause**: Blender only exported the currently active Action.
**Fix**:
1. Open the NLA Editor (change editor type to NLA Editor).
2. For each action, press **Shift+Click** on the action strip to push it to the NLA.
3. Make sure all strips are enabled (not muted).
4. Re-export.

### Problem: Mixamo rigged wrong (fingers through head, arms misaligned)
**Fix**: Re-upload to Mixamo and manually position the dots:
- Chin dot → under the chin
- Left wrist → inside of left wrist
- Right wrist → inside of right wrist
- Left elbow → outside of left elbow
- Right elbow → outside of right elbow
- Use symmetric mode for left/right.

### Problem: animation_name not matching in Three.js
Canary's viewer.html uses these **exact** clip names (lowercase, underscores):
```
idle, chin_tuck, head_up, head_tilt_left, head_tilt_right,
head_look_left, head_look_right, head_rotation,
leg_raise_left, leg_raise_right, lateral_bend, knee_raise,
shoulder_socket_rotation, shoulder_drop, thoracic_rotation,
arm_raises, leg_extension, heel_raises
```
Check in gltf.report that your names match exactly.
If not, rename the Actions in Blender's Action Editor and re-export.

---

## Repeat for girl.blend → canary_girl.glb

Follow the exact same steps.
The girl model can share all the same Mixamo animations —
just upload her mesh to Mixamo separately and download the same
animation set, since body proportions affect how animations look.

---

## Final file checklist

```
canary/assets/
├── canary_boy.glb   ← boy mesh + all 17 named animations + idle
├── canary_girl.glb  ← girl mesh + all 17 named animations + idle
└── tree_trunk.glb   ← from Meshy (no animation needed)
```

Total size target: each GLB should be under 15 MB.
If larger, in the GLB export settings enable **Compression (Draco)**.
