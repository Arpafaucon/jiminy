import io
import os
import re
import sys
import math
import array
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Callable, Optional, Dict, Tuple, Union, Sequence, Any, List

import numpy as np

import simplepbr
from panda3d.core import (
    NodePath, Point3, Vec3, Vec4, Mat4, Quat, LQuaternion, Geom, GeomEnums,
    GeomNode, GeomTriangles, GeomVertexData, GeomVertexArrayFormat,
    GeomVertexFormat, GeomVertexWriter, PNMImage, PNMImageHeader, TextNode,
    OmniBoundingVolume, CompassEffect, BillboardEffect, InternalName, Filename,
    Material, Texture, TextureStage, TransparencyAttrib, PGTop, Camera,
    PerspectiveLens, OrthographicLens, Shader, ShaderAttrib, AntialiasAttrib,
    CollisionNode, CollisionRay, CollisionTraverser, CollisionHandlerQueue,
    ClockObject, GraphicsPipe, GraphicsOutput, GraphicsWindow,
    RenderModeAttrib, WindowProperties, FrameBufferProperties, loadPrcFileData)
from direct.showbase.ShowBase import ShowBase
from direct.gui.OnscreenImage import OnscreenImage
from direct.gui.OnscreenText import OnscreenText

import panda3d_viewer
import panda3d_viewer.viewer
import panda3d_viewer.viewer_app
import panda3d_viewer.viewer_proxy
from panda3d_viewer import geometry
from panda3d_viewer.viewer_errors import ViewerClosedError
from panda3d_viewer.viewer_config import ViewerConfig

import hppfcl
import pinocchio as pin
from pinocchio.utils import npToTuple
from pinocchio.visualize import BaseVisualizer


WINDOW_SIZE_DEFAULT = (800, 800)
CAMERA_POS_DEFAULT = [(4.0, -4.0, 1.5), (0, 0, 0.5)]

LEGEND_DPI = 400
LEGEND_SCALE = 0.3
CLOCK_SCALE = 0.1
WIDGET_MARGIN_REL = 0.05

PANDA3D_FRAMERATE_MAX = 40


Tuple3FType = Union[Tuple[float, float, float], np.ndarray]
Tuple4FType = Union[Tuple[float, float, float, float], np.ndarray]
FrameType = Union[Tuple[Tuple3FType, Tuple4FType], np.ndarray]


def _sanitize_path(path: str) -> str:
    """Sanitize path on windows to make it compatible with python bindings.

    Assimp bindings used to load meshes and other C++ tools handling path does
    not support several features on Windows. First, it does not support
    symlinks, then the hard drive prefix must be `/x/` instead of `X:\\`,
    folder's name must respect the case, and backslashes must be used as
    delimiter instead of forward slashes.

    :param path: Path to sanitize.
    """
    if sys.platform.startswith('win'):
        path = os.path.realpath(path)
        path = PureWindowsPath(path).as_posix()
        path = re.sub(r'^([A-Za-z]):',
                      lambda m: "/" + m.group(1).lower(),
                      path)
    return path


def make_gradient_skybox(sky_color: Tuple3FType,
                         ground_color: Tuple3FType,
                         offset: float = 0.0,
                         subdiv: int = 2):
    """Simple gradient to be used as skybox.

    For reference, see:
    - https://discourse.panda3d.org/t/color-gradient-scene-background/26946/14
    """
    # Check validity of arguments
    assert subdiv >= 2, "Number of sub-division must be larger than 2."
    assert 0.0 <= offset and offset <= 1.0, "Offset must be in [0.0, 1.0]."

    # Define vertex format
    vformat = GeomVertexFormat()
    aformat = GeomVertexArrayFormat()
    aformat.add_column(
        InternalName.get_vertex(), 3, Geom.NT_float32, Geom.C_point)
    vformat.add_array(aformat)
    aformat = GeomVertexArrayFormat()
    aformat.add_column(
        InternalName.make("color"), 4, Geom.NT_uint8, Geom.C_color)
    vformat.add_array(aformat)
    vformat = GeomVertexFormat.register_format(vformat)

    # Create a simple, horizontal prism.
    # Make it very wide to avoid ever seeing its left and right sides.
    # One edge is at the "horizon", while the two other edges are above
    # and a bit behind the camera so they are only visible when looking
    # straight up.
    vertex_data = GeomVertexData(
        "prism_data", vformat, GeomEnums.UH_static)
    vertex_data.unclean_set_num_rows(4 + subdiv * 2)
    values = array.array("f", (-1000., -50., 86.6, 1000., -50., 86.6))
    offset_angle = np.pi / 1.5 * offset
    delta_angle = (np.pi / .75 - offset_angle * 2.) / (subdiv + 1)
    for i in range(subdiv):
        angle = np.pi / 3. + offset_angle + delta_angle * (i + 1)
        y = -np.cos(angle) * 100.
        z = np.sin(angle) * 100.
        values.extend((-1000., y, z, 1000., y, z))
    values.extend((-1000., -50., -86.6, 1000., -50., -86.6))
    pos_array = vertex_data.modify_array(0)
    memview = memoryview(pos_array).cast("B").cast("f")
    memview[:] = values

    # Interpolate the colors
    color1 = tuple(int(c * 255) for c in sky_color)
    color2 = tuple(int(c * 255) for c in ground_color)
    values = array.array("B", color1 * 2)
    for ratio in np.linspace(0, 1, subdiv):
        color = tuple(int(c1 * (1 - ratio) + c2 * ratio)
                      for c1, c2 in zip(color1, color2))
        values.extend(color * 2)
    values.extend(color2 * 2)
    color_array = vertex_data.modify_array(1)
    memview = memoryview(color_array).cast("B")
    memview[:] = values

    tris_prim = GeomTriangles(GeomEnums.UH_static)
    indices = array.array("H", (0, 3, 1, 0, 2, 3))
    for i in range(subdiv + 1):
        j = i * 2
        indices.extend((j, 3 + j, 1 + j, j, 2 + j, 3 + j))
    j = (subdiv + 1) * 2
    indices.extend((j, 1, 1 + j, j, 0, 1))
    tris_array = tris_prim.modify_vertices()
    tris_array.unclean_set_num_rows((subdiv + 3) * 6)
    memview = memoryview(tris_array).cast("B").cast("H")
    memview[:] = indices

    # The compass effect can make the node leave its bounds, so make them
    # infinitely large.
    geom = Geom(vertex_data)
    geom.add_primitive(tris_prim)
    node = GeomNode("prism")
    node.add_geom(geom)
    node.set_bounds(OmniBoundingVolume())
    prism = NodePath(node)
    prism.set_bin("background", 0)
    prism.set_depth_write(False)
    prism.set_depth_test(False)

    return prism


def make_cone(num_sides: int = 16) -> Geom:
    """Create a close shaped cone, approximate by a pyramid with regular
    convex n-sided polygon base.

    For reference about regular polygon:
    https://en.wikipedia.org/wiki/Regular_polygon
    """
    # Define vertex format
    vformat = GeomVertexFormat.get_v3n3()
    vdata = GeomVertexData('vdata', vformat, Geom.UH_static)
    vdata.unclean_set_num_rows(num_sides + 3)
    vertex = GeomVertexWriter(vdata, 'vertex')
    normal = GeomVertexWriter(vdata, 'normal')

    # Add radial points
    for u in np.linspace(0.0, 2 * np.pi, num_sides + 1):
        x, y = math.cos(u), math.sin(u)
        vertex.addData3(x, y, 0.0)
        normal.addData3(x, y, 0.0)

    # Add top and bottom points
    vertex.addData3(0.0, 0.0, 1.0)
    normal.addData3(0.0, 0.0, 1.0)
    vertex.addData3(0.0, 0.0, 0.0)
    normal.addData3(0.0, 0.0, -1.0)

    # Make triangles.
    # Note that by default, rendering is one-sided. It only renders the outside
    # face, that is defined based on the "winding" order of the vertices making
    # the triangles. For reference, see:
    # https://discourse.panda3d.org/t/procedurally-generated-geometry-and-the-default-normals/24986/2
    prim = GeomTriangles(Geom.UH_static)
    prim.reserveNumVertices(6 * num_sides)
    for i in range(num_sides):
        prim.add_vertices(i, i + 1, num_sides + 1)
        prim.add_vertices(i + 1, i, num_sides + 2)

    # Create geometry object
    geom = Geom(vdata)
    geom.add_primitive(prim)

    return geom


def make_heightmap(heightmap: np.ndarray) -> Geom:
    """Create height map.
    """
    # Compute the number of vertices and triangles, assuming it is square
    dim = int(math.sqrt(heightmap.shape[0]))
    num_vertices = int(dim * dim)
    num_triangles = int(2 * (dim - 1) ** 2)

    # Allocation vertex
    vformat = GeomVertexFormat.get_v3n3()
    vdata = GeomVertexData('vdata', vformat, Geom.UH_static)
    vdata.unclean_set_num_rows(num_vertices)

    # Set vertex data
    vdata_view = memoryview(vdata.modify_array(0)).cast("B")
    vdata_view[:] = array.array("f", heightmap.reshape((-1,))).tobytes()

    # Make triangles
    prim = GeomTriangles(Geom.UH_static)
    prim.set_index_type(Geom.NT_uint32)
    tris_array = prim.modify_vertices()
    indices = np.empty((num_triangles, 3), dtype=np.uint32)
    tri_idx = 0
    for i in range(dim - 1):
        for j in range(1, dim - 1):
            k = j * dim + i
            indices[tri_idx] = k, k + 1, k + dim
            indices[tri_idx + 1] = k + 1, k, k + 1 - dim
            tri_idx += 2
        k = (dim - 1) * dim + i
        indices[tri_idx] = i, i + 1, i + dim
        indices[tri_idx + 1] = k + 1, k, k + 1 - dim
        tri_idx += 2
    tris_array.unclean_set_num_rows(indices.size)
    memview = memoryview(tris_array)
    memview[:] = array.array("I", indices.reshape((-1,)))

    # Create geometry object
    geom = Geom(vdata)
    geom.add_primitive(prim)

    return geom


class Panda3dApp(panda3d_viewer.viewer_app.ViewerApp):
    def __init__(self, config: Optional[ViewerConfig] = None) -> None:
        # Enforce viewer configuration
        if config is None:
            config = ViewerConfig()
        config.set_window_size(*WINDOW_SIZE_DEFAULT)
        config.set_window_fixed(False)
        config.enable_antialiasing(True, multisamples=2)
        config.set_value('framebuffer-software', '0')
        config.set_value('framebuffer-hardware', '0')
        config.set_value('load-display', 'pandagl')
        config.set_value('aux-display',
                         'p3headlessgl'
                         '\naux-display pandadx9'
                         '\naux-display p3tinydisplay')
        config.set_value('window-type', 'offscreen')
        config.set_value('sync-video', '0')
        config.set_value('default-near', 0.1)
        config.set_value('gl-version', '3 1')
        config.set_value('notify-level', 'fatal')
        config.set_value('notify-level-x11display', 'fatal')
        config.set_value('notify-level-device', 'fatal')
        config.set_value('default-directnotify-level', 'error')
        loadPrcFileData('', str(config))

        # Define offscreen buffer
        self.buff = None

        # Initialize base implementation.
        # Note that the original constructor is by-passed on purpose.
        ShowBase.__init__(self)

        # Monkey-patch task manager to ignore SIGINT from keyboard interrupt
        def keyboardInterruptHandler(signalNumber, stackFrame):
            pass

        self.taskMgr.keyboardInterruptHandler = keyboardInterruptHandler

        # Disable the task manager loop for now. Only useful if onscreen.
        self.shutdown()

        # Active enhanced rendering if discrete NVIDIA GPU is used.
        # Note that shadow resolution larger than 1024 significantly affects
        # the frame rate on Intel GPU chipsets: going from 1024 to 2048 makes
        # it drop from 60FPS to 30FPS.
        if self.win.gsg.driver_vendor.startswith('NVIDIA'):
            self._shadow_size = 4096
        else:
            self._shadow_size = 1024

        # Enable antialiasing
        self.render.set_antialias(AntialiasAttrib.MMultisample)

        # Configure lighting and shadows
        self._spotlight = self.config.GetBool('enable-spotlight', False)
        self._lights_mask = [True, True]

        # Create physics-based shader and adapt lighting accordingly.
        # It slows down the rendering by about 30% on discrete NVIDIA GPU.
        shader_options = {'ENABLE_SHADOWS': ''}
        pbr_vert = simplepbr._load_shader_str('simplepbr.vert', shader_options)
        pbr_frag = simplepbr._load_shader_str('simplepbr.frag', shader_options)
        pbrshader = Shader.make(
            Shader.SL_GLSL, vertex=pbr_vert, fragment=pbr_frag)
        self.render.set_attrib(ShaderAttrib.make(pbrshader))
        self._lights = [
            self._make_light_ambient((0.5, 0.5, 0.5)),
            self._make_light_direct(1, (1.0, 1.0, 1.0), pos=(8.0, -8.0, 10.0))]

        # Define default camera pos
        self._camera_defaults = CAMERA_POS_DEFAULT
        self.reset_camera(*self._camera_defaults)

        # Define clock. It will be used later to limit framerate
        self.clock = ClockObject.get_global_clock()
        self.framerate = None

        # Create scene tree
        self._scene_root = self.render.attach_new_node('scene_root')
        self._scene_scale = self.config.GetFloat('scene-scale', 1.0)
        self._scene_root.set_scale(self._scene_scale)
        self._groups = {}

        # Create default scene objects
        self._fog = self._make_fog()
        self._axes = self._make_axes()
        self._grid = self._make_grid()
        self._floor = self._make_floor()

        # Create gradient for skybox
        sky_color = (0.53, 0.8, 0.98, 1.0)
        ground_color = (0.1, 0.1, 0.43, 1.0)
        self.skybox = make_gradient_skybox(sky_color, ground_color, 0.7)
        self.skybox.set_shader_auto(True)
        self.skybox.set_light_off()
        self.skybox.hide(self.LightMask)

        # The background needs to be parented to an intermediary node to which
        # a compass effect is applied to keep it at the same position as the
        # camera, while being parented to render.
        pivot = self.render.attach_new_node("pivot")
        effect = CompassEffect.make(self.camera, CompassEffect.P_pos)
        pivot.set_effect(effect)
        self.skybox.reparent_to(pivot)

        # The background needs to keep facing the camera a point behind the
        # camera. Note that only its heading should correspond to that of the
        # camera, while the pitch and roll remain unaffected.
        effect = BillboardEffect.make(
            Vec3.up(), False, True, 0.0, NodePath(),
            Point3(0.0, -10.0, 0.0), False)
        self.skybox.set_effect(effect)

        # Create shared 2D renderer to allow display selectively gui elements
        # on offscreen and onscreen window used for capturing frames.
        self.shared_render_2d = NodePath('shared_render_2d')
        self.shared_render_2d.set_depth_test(False)
        self.shared_render_2d.set_depth_write(False)

        # Create dedicated camera 2D for offscreen rendering
        self.offscreen_camera_2d = NodePath(Camera('offscreen_camera2d'))
        lens = OrthographicLens()
        lens.set_film_size(2, 2)
        lens.set_near_far(-1000, 1000)
        self.offscreen_camera_2d.node().set_lens(lens)
        self.offscreen_camera_2d.reparent_to(self.shared_render_2d)

        # Create dedicated aspect2d for offscreen rendering
        self.offAspect2d = self.shared_render_2d.attach_new_node(
            PGTop("offAspect2d"))
        self.offA2dTopLeft = self.offAspect2d.attach_new_node(
            "offA2dTopLeft")
        self.offA2dTopRight = self.offAspect2d.attach_new_node(
            "offA2dTopRight")
        self.offA2dBottomLeft = self.offAspect2d.attach_new_node(
            "offA2dBottomLeft")
        self.offA2dBottomCenter = self.offAspect2d.attach_new_node(
            "offA2dBottomCenter")
        self.offA2dBottomRight = self.offAspect2d.attach_new_node(
            "offA2dBottomRight")
        self.offA2dTopLeft.set_pos(self.a2dLeft, 0, self.a2dTop)
        self.offA2dTopRight.set_pos(self.a2dRight, 0, self.a2dTop)
        self.offA2dBottomLeft.set_pos(self.a2dLeft, 0, self.a2dBottom)
        self.offA2dBottomCenter.set_pos(0, 0, self.a2dBottom)
        self.offA2dBottomRight.set_pos(self.a2dRight, 0, self.a2dBottom)

        # Define widget overlay
        self.offscreen_graphics_lens = None
        self.offscreen_display_region = None
        self._help_label = None
        self._watermark = None
        self._legend = None
        self._clock = None

        # Define input control
        self.key_map = {"mouse1": 0, "mouse2": 0, "mouse3": 0}

        # Define camera control
        self.zoom_rate = 1.03
        self.camera_lookat = np.zeros(3)
        self.longitude_deg = 0.0
        self.latitude_deg = 0.0
        self.last_mouse_x = 0.0
        self.last_mouse_y = 0.0

        # Define object/highlighting selector
        self.picker_ray = None
        self.picker_node = None
        self.picked_object = None
        self.click_mouse_x = 0.0
        self.click_mouse_y = 0.0

        # Create resizeable offscreen buffer.
        # Note that a resizable buffer is systematically created, no matter
        # if the main window is an offscreen non-resizable window or an
        # onscreen resizeable graphical window. It avoids having to handle
        # the two cases separately, especially for screenshot resizing and
        # selective overlay information display. However, it affects the
        # performance significantly. At least 20% on discrete NVIDIA GPU and
        # 50% on integrated Intel GPU.
        self._open_offscreen_window(WINDOW_SIZE_DEFAULT)

        # Set default options
        self.enable_lights(True)
        self.enable_shadow(True)
        self.enable_hdr(False)
        self.enable_fog(False)
        self.show_axes(True)
        self.show_grid(False)
        self.show_floor(True)

    def has_gui(self) -> bool:
        return any(isinstance(win, GraphicsWindow) for win in self.winList)

    def open_window(self) -> None:
        """Open a graphical window, with offscreen buffer attached on it to
        allow for arbitrary size screenshot.
        """
        # Make sure a graphical window is not already open
        if self.has_gui():
            raise RuntimeError("Only one graphical window can be opened.")

        # Replace the original offscreen window by an onscreen one if possible
        is_success = True
        size = self.win.get_size()
        try:
            self.windowType = 'onscreen'
            self.open_main_window(size=size)
        except Exception:   # pylint: disable=broad-except
            is_success = False
            self.windowType = 'offscreen'
            self.open_main_window(size=size)

        if is_success:
            # Setup mouse and keyboard controls for onscreen display
            self._setup_shortcuts()
            self.disableMouse()
            self.accept("wheel_up", self.handle_key, ["wheelup", 1])
            self.accept("wheel_down", self.handle_key, ["wheeldown", 1])
            for i in range(1, 4):
                self.accept(f"mouse{i}", self.handle_key, [f"mouse{i}", 1])
                self.accept(f"mouse{i}-up", self.handle_key, [f"mouse{i}", 0])
            self.taskMgr.add(
                self.move_orbital_camera_task, "move_camera_task", sort=2)

            # Setup object pickler
            self.picker_ray = CollisionRay()
            self.picker_node = CollisionNode('mouse_ray')
            self.picker_node.set_from_collide_mask(
                GeomNode.get_default_collide_mask())
            self.picker_node.addSolid(self.picker_ray)
            self.picker_traverser = CollisionTraverser('traverser')
            picker_np = self.camera.attachNewNode(self.picker_node)
            self.picker_queue = CollisionHandlerQueue()
            self.picker_traverser.addCollider(picker_np, self.picker_queue)

            # Limit framerate to reduce computation cost
            self.set_framerate(PANDA3D_FRAMERATE_MAX)

        # Create resizeable offscreen buffer
        self._open_offscreen_window(size)

        # Throw exception if opening display has failed
        if not is_success:
            raise RuntimeError(
                "Impossible to open graphical window. Make sure display is "
                "available on system.")

        # Enable the task manager
        self.restart()

    def _open_offscreen_window(self,
                               size: Optional[Tuple[int, int]] = None) -> None:
        """Create new completely independent offscreen buffer, rendering the
        same scene than the main window.
        """
        # Handling of default size
        if size is None:
            size = self.win.get_size()

        # Close existing offscreen display if any.
        # Note that one must remove display region associated with shared 2D
        # renderer, otherwise it will be altered when closing current window.
        if self.buff is not None:
            self.buff.remove_display_region(self.offscreen_display_region)
            self.close_window(self.buff, keepCamera=False)

        # Set offscreen buffer frame properties
        # Note that accumulator bits and back buffers is not supported by
        # resizeable buffers.
        fbprops = FrameBufferProperties(self.win.getFbProperties())
        fbprops.set_accum_bits(0)
        fbprops.set_back_buffers(0)

        # Set offscreen buffer windows properties
        winprops = WindowProperties()
        winprops.set_size(*size)

        # Set offscreen buffer flags to enforce resizeable `GraphicsBuffer`
        flags = GraphicsPipe.BF_refuse_window | GraphicsPipe.BF_refuse_parasite
        flags |= GraphicsPipe.BF_resizeable

        # Create new offscreen buffer
        # Note that it is impossible to create resizeable buffer without an
        # already existing host.
        win = self.graphicsEngine.make_output(
            self.pipe, "offscreen_buffer", 0, fbprops, winprops, flags,
            self.win.get_gsg(), self.win)
        self.buff = win

        # Append buffer to the list of windows managed by the ShowBase
        self.winList.append(win)

        # Attach a texture as screenshot requires copying GPU data to RAM
        texture = Texture()
        self.buff.add_render_texture(texture, GraphicsOutput.RTM_copy_ram)

        # Create 3D camera region for the scene.
        # Set near distance of camera lens to allow seeing model from close.
        self.offscreen_graphics_lens = PerspectiveLens()
        self.offscreen_graphics_lens.set_near(0.1)
        self.make_camera(
            win, camName='offscreen_camera', lens=self.offscreen_graphics_lens)

        # Create 2D display region for widgets
        self.offscreen_display_region = win.makeMonoDisplayRegion()
        self.offscreen_display_region.set_sort(5)
        self.offscreen_display_region.set_camera(self.offscreen_camera_2d)

        # Adjust aspect ratio
        self._adjust_offscreen_window_aspect_ratio()

        # Force rendering the scene to finalize initialization of the GSG
        self.graphics_engine.render_frame()

        # The buffer must be flipped upside-down manually because using the
        # global option `copy-texture-inverted` distorts the shadows of the
        # onscreen window for some reason. Moreover, it must be done after
        # calling `render_frame` at least once.
        self.buff.inverted = True

    def _adjust_offscreen_window_aspect_ratio(self) -> None:
        """Adjust aspect ratio of offscreen window.

        .. note::
            This method is called automatically after resize.
        """
        # Get aspect ratio
        aspect_ratio = self.get_aspect_ratio(self.buff)

        # Adjust 3D rendering aspect ratio
        self.offscreen_graphics_lens.set_aspect_ratio(aspect_ratio)

        # Adjust existing anchors for offscreen 2D rendering
        if aspect_ratio < 1:
            # If the window is TALL, lets expand the top and bottom
            self.offAspect2d.set_scale(1.0, aspect_ratio, aspect_ratio)
            a2dTop = 1.0 / aspect_ratio
            a2dBottom = - 1.0 / aspect_ratio
            a2dLeft = -1
            a2dRight = 1.0
        else:
            # If the window is WIDE, lets expand the left and right
            self.offAspect2d.set_scale(1.0/aspect_ratio, 1.0, 1.0)
            a2dTop = 1.0
            a2dBottom = -1.0
            a2dLeft = -aspect_ratio
            a2dRight = aspect_ratio

        self.offA2dTopLeft.set_pos(a2dLeft, 0, a2dTop)
        self.offA2dTopRight.set_pos(a2dRight, 0, a2dTop)
        self.offA2dBottomLeft.set_pos(a2dLeft, 0, a2dBottom)
        self.offA2dBottomRight.set_pos(a2dRight, 0, a2dBottom)

    def getSize(self, win: Optional[Any] = None) -> Tuple[int, int]:
        """Patched to return the size of the window used for capturing frame by
        default, instead of main window.
        """
        if win is None:
            win = self.buff
        return super().getSize(win)

    def getMousePos(self) -> Tuple[float, float]:
        """Get current mouse position.

        .. note::
            Can be overloaded to allow for emulated mouse click.
        """
        md = self.win.getPointer(0)
        return md.getX(), md.getY()

    def handle_key(self, key: str, value: bool) -> None:
        """Input controller handler.
        """
        if key in ["mouse1", "mouse2", "mouse3"]:
            mouseX, mouseY = self.getMousePos()
            if key == "mouse1":
                if value:
                    self.click_mouse_x, self.click_mouse_y = mouseX, mouseY
                elif (self.click_mouse_x == mouseX and
                        self.click_mouse_y == mouseY):
                    # Do not enable clicking on node for Qt widget since
                    # mouse watcher and picker are not properly configured.
                    if self.picker_ray is not None:
                        self.click_on_node()
            self.last_mouse_x, self.last_mouse_y = mouseX, mouseY
            self.key_map[key] = value
        elif key in ["wheelup", "wheeldown"]:
            cam_dir = self.camera_lookat - np.asarray(self.camera.get_pos())
            if key == "wheelup":
                cam_pos = self.camera_lookat - cam_dir / self.zoom_rate
            else:
                cam_pos = self.camera_lookat - cam_dir * self.zoom_rate
            self.camera.set_pos(*cam_pos)

    def click_on_node(self) -> None:
        """Object selector handler.
        """
        # Remove focus of currently selected object
        picked_object_prev = self.picked_object
        if self.picked_object is not None:
            self.highlight_node(*self.picked_object, False)
            self.picked_object = None

        # Select new object if the user actually clicked on a selectable node
        object_found = False
        mpos = self.mouseWatcherNode.getMouse()
        self.picker_ray.set_from_lens(self.camNode, mpos.getX(), mpos.getY())
        self.picker_traverser.traverse(self.render)
        for i in range(self.picker_queue.getNumEntries()):
            self.picker_queue.sortEntries()
            picked_node = self.picker_queue.getEntry(i).getIntoNodePath()
            # Do not allow selecting hidden node
            if not picked_node.isHidden():
                node_path = str(picked_node)
                for group_name in self._groups.keys():
                    group_path = f"render/scene_root/{group_name}/"
                    # Only nodes part of user groups can be selected
                    if node_path.startswith(group_path):
                        name = node_path[len(group_path):]
                        if (group_name, name) != picked_object_prev:
                            self.picked_object = (group_name, name)
                        object_found = True
                        break
                if object_found:
                    break

        # Focus on newly selected node
        if self.picked_object is not None:
            self.highlight_node(*self.picked_object, True)

    def move_orbital_camera_task(self,
                                 task: Optional[Any] = None) -> Optional[int]:
        """Custom control of the camera to be more suitable for robotic
        application than the default one.
        """
        # Get mouse position
        x, y = self.getMousePos()

        # Ensure consistent camera pose and lookat
        self.longitude_deg, self.latitude_deg, _ = self.camera.get_hpr()
        cam_pos = np.asarray(self.camera.get_pos())
        cam_dir = self.camera_lookat - cam_pos
        cam_dist = np.linalg.norm(cam_dir)
        longitude = self.longitude_deg * np.pi / 180.0
        latitude = self.latitude_deg * np.pi / 180.0
        cam_dir_n = np.array([-np.sin(longitude)*np.cos(latitude),
                              np.cos(longitude)*np.cos(latitude),
                              np.sin(latitude)])
        self.camera_lookat = cam_pos + cam_dist * cam_dir_n

        if self.key_map["mouse1"]:
            # Update camera rotation
            self.longitude_deg -= (x - self.last_mouse_x) * 0.2
            self.latitude_deg -= (y - self.last_mouse_y) * 0.2

            # Limit angles to [-180;+180] x [-90;+90]
            if (self.longitude_deg > 180.0):
                self.longitude_deg = self.longitude_deg - 360.0
            if (self.longitude_deg < -180.0):
                self.longitude_deg = self.longitude_deg + 360.0
            if (self.latitude_deg > (90.0 - 0.001)):
                self.latitude_deg = 90.0 - 0.001
            if (self.latitude_deg < (-90.0 + 0.001)):
                self.latitude_deg = -90.0 + 0.001

            longitude = self.longitude_deg * np.pi / 180.0
            latitude = self.latitude_deg * np.pi / 180.0
            cam_dir_n = np.array([-np.sin(longitude) * np.cos(latitude),
                                  np.cos(longitude) * np.cos(latitude),
                                  np.sin(latitude)])
            cam_pos = self.camera_lookat - cam_dist * cam_dir_n
            self.camera.set_pos(*cam_pos)
            self.camera.set_hpr(self.longitude_deg, self.latitude_deg, 0)
        if self.key_map["mouse2"]:
            cam_delta = (y - self.last_mouse_y) * 0.02 * cam_dir_n
            self.camera_lookat -= cam_delta
            cam_pos -= cam_delta
            self.camera.set_pos(*cam_pos)
        elif self.key_map["mouse3"]:
            cam_n1 = np.array([np.cos(longitude),
                               np.sin(longitude),
                               0.0])
            cam_n2 = np.array([-np.sin(longitude) * np.sin(latitude),
                               np.cos(longitude) * np.sin(latitude),
                               -np.cos(latitude)])
            pos_shift = ((x - self.last_mouse_x) * cam_n1 +
                         (y - self.last_mouse_y) * cam_n2) * 0.01
            cam_pos -= pos_shift
            self.camera_lookat -= pos_shift
            self.camera.set_pos(*cam_pos)

        # Store latest mouse position for the next frame
        self.last_mouse_x = x
        self.last_mouse_y = y

        # End task
        if task is not None:
            return task.cont

    def _make_light_ambient(self, color: Tuple3FType) -> NodePath:
        """Patched to fix wrong color alpha.
        """
        node = super()._make_light_ambient(color)
        node.get_node(0).set_color((*color, 1.0))
        return node

    def _make_light_direct(self,
                           index: int,
                           color: Tuple3FType,
                           pos: Tuple3FType,
                           target: Tuple3FType = (0.0, 0.0, 0.0)
                           ) -> NodePath:
        """Patched to fix wrong color alpha.
        """
        light_path = super()._make_light_direct(index, color, pos, target)
        light_path.get_node(0).set_color((*color, 1.0))
        return light_path

    def _make_axes(self) -> NodePath:
        model = GeomNode('axes')
        model.add_geom(geometry.make_axes())
        node = self.render.attach_new_node(model)
        node.set_render_mode_wireframe()
        if self.win.gsg.driver_vendor.startswith('NVIDIA'):
            node.set_render_mode_thickness(4)
        node.set_antialias(AntialiasAttrib.MLine)
        node.set_shader_auto(True)
        node.set_light_off()
        node.hide(self.LightMask)
        node.set_scale(0.3)
        return node

    def _make_floor(self,
                    heightmap: Optional[np.ndarray] = None,
                    show_meshes: bool = False) -> NodePath:
        model = GeomNode('floor')
        node = self.render.attach_new_node(model)

        if heightmap is None:
            for xi in range(-10, 11):
                for yi in range(-10, 11):
                    tile = GeomNode(f"tile-{xi}.{yi}")
                    tile.add_geom(geometry.make_plane(size=(1.0, 1.0)))
                    tile_path = node.attach_new_node(tile)
                    tile_path.set_pos((xi, yi, 0.0))
                    if (xi + yi) % 2:
                        tile_path.set_color((0.95, 0.95, 1.0, 1.0))
                    else:
                        tile_path.set_color((0.13, 0.13, 0.2, 1.0))
        else:
            model.add_geom(make_heightmap(heightmap))
            node.set_color((0.75, 0.75, 0.85, 1.0))
            if show_meshes:
                render_attrib = node.get_state().get_attrib_def(
                    RenderModeAttrib.get_class_slot())
                node.set_attrib(RenderModeAttrib.make(
                    RenderModeAttrib.M_filled_wireframe,
                    0.5,  # thickness
                    render_attrib.perspective,
                    (0.7, 0.7, 0.7, 1.0)  # wireframe_color
                ))

        # Make the floor two-sided to not see through from below
        node.set_two_sided(True)

        # Set material to render shadows if supported
        material = Material()
        material.set_base_color((1.35, 1.35, 1.35, 1.0))
        node.set_material(material, True)

        # Disable light casting
        node.hide(self.LightMask)

        # Adjust frustum of the lights to project shadow over the whole scene
        for light_path in self._lights[1:]:
            bmin, bmax = node.get_tight_bounds(light_path)
            lens = light_path.get_node(0).get_lens()
            lens.set_film_offset((bmin.xz + bmax.xz) * 0.5)
            lens.set_film_size(bmax.xz - bmin.xz)
            lens.set_near_far(bmin.y, bmax.y)

        return node

    def update_floor(self,
                     heightmap: Optional[np.ndarray] = None,
                     show_meshes: bool = False) -> NodePath:
        """Update the floor.

        :param heightmap: Height map of the ground, as a 2D nd.array of shape
                          [N_X * N_Y, 6], where N_X, N_Y are the number of
                          vertices on x and y axes respectively, while the
                          last dimension corresponds to the position (x, y, z)
                          and normal (n_x, n_y, nz) of the vertex in space. It
                          renders a flat tile ground if not specified.
                          Optional: None by default.
        """
        # Check if floor is currently hidden
        is_hidden = self._floor.isHidden()

        # Remove existing floor and create a new one
        self._floor.remove_node()
        self._floor = self._make_floor(heightmap, show_meshes)

        # Hide the floor if is was previously hidden
        if is_hidden:
            self._floor.hide()

    def append_group(self,
                     root_path: str,
                     remove_if_exists: bool = True,
                     scale: float = 1.0) -> None:
        """Patched to avoid adding new group if 'remove_if_exists' is false,
        otherwise it will be impossible to access to old ones.
        """
        if not remove_if_exists and root_path in self._groups:
            return
        super().append_group(root_path, remove_if_exists, scale)

    def append_node(self,
                    root_path: str,
                    name: str,
                    node: NodePath,
                    frame: Optional[FrameType] = None) -> None:
        """Patched to make sure node's name is valid and set the color scale.
        """
        assert re.match(r'^[A-Za-z0-9_]+$', name), (
            "Node's name is restricted to case-insensitive ASCII alphanumeric "
            "string (including underscores).")
        node.set_color_scale((1.2, 1.2, 1.2, 1.0))
        super().append_node(root_path, name, node, frame)

    def highlight_node(self, root_path: str, name: str, enable: bool) -> None:
        node = self._groups[root_path].find(name)
        if node:
            render_mode = node.get_render_mode()
            if enable:
                if render_mode == RenderModeAttrib.M_filled_wireframe:
                    return
                render_attrib = node.get_state().get_attrib_def(
                    RenderModeAttrib.get_class_slot())
                node.set_attrib(RenderModeAttrib.make(
                    RenderModeAttrib.M_filled_wireframe,
                    1.0,  # thickness (1.0 by default)
                    render_attrib.perspective,
                    (2.0, 2.0, 2.0, 1.0)  # wireframe_color
                ))
            else:
                if render_mode == RenderModeAttrib.M_off:
                    return
                node.clear_render_mode()

    def append_frame(self,
                     root_path: str,
                     name: str,
                     frame: Optional[FrameType] = None) -> None:
        """Append a cartesian frame primitive node to the group.
        """
        model = GeomNode('axes')
        model.add_geom(geometry.make_axes())
        node = NodePath(model)
        node.set_render_mode_wireframe()
        if self.win.gsg.driver_vendor.startswith('NVIDIA'):
            node.set_render_mode_thickness(4)
        node.set_antialias(AntialiasAttrib.MLine)
        node.set_shader_auto(True)
        node.set_light_off()
        node.hide(self.LightMask)
        self.append_node(root_path, name, node, frame)

    def append_cone(self,
                    root_path: str,
                    name: str,
                    radius: float,
                    length: float,
                    num_sides: int = 16,
                    frame: Optional[FrameType] = None) -> None:
        """Append a cone primitive node to the group.
        """
        geom_node = GeomNode("cone")
        geom_node.add_geom(make_cone(num_sides))
        node = NodePath(geom_node)
        node.set_scale(radius, radius, length)
        self.append_node(root_path, name, node, frame)

    def append_cylinder(self,
                        root_path: str,
                        name: str,
                        radius: float,
                        length: float,
                        anchor_bottom: bool = False,
                        frame: Optional[FrameType] = None) -> None:
        """Patched to add optional to place anchor at the bottom of the
        cylinder instead of the middle.
        """
        geom_node = GeomNode('cylinder')
        geom_node.add_geom(geometry.make_cylinder())
        node = NodePath(geom_node)
        node.set_scale(Vec3(radius, radius, length))
        if anchor_bottom:
            node.set_pos(0.0, 0.0, -length/2)
        self.append_node(root_path, name, node, frame)

    def append_arrow(self,
                     root_path: str,
                     name: str,
                     radius: float,
                     length: float,
                     frame: Optional[FrameType] = None) -> None:
        """Append an arrow primitive node to the group.

        ..note::
            The arrow is aligned with z-axis in world frame, and the tip is at
            position (0.0, 0.0, 0.0) in world frame.
        """
        arrow_geom = GeomNode("arrow")
        arrow_node = NodePath(arrow_geom)
        head = make_cone()
        head_geom = GeomNode("head")
        head_geom.addGeom(head)
        head_node = NodePath(head_geom)
        head_node.reparent_to(arrow_node.attach_new_node("head"))
        head_node.set_scale(1.75, 1.75, 3.5*radius)
        head_node.set_pos(0.0, 0.0, -3.5*radius)
        body = geometry.make_cylinder()
        body_geom = GeomNode("body")
        body_geom.addGeom(body)
        body_node = NodePath(body_geom)
        body_node.reparent_to(arrow_node.attach_new_node("body"))
        body_node.set_scale(1.0, 1.0, length)
        body_node.set_pos(0.0, 0.0, -length/2-3.5*radius)
        arrow_node.set_scale(radius, radius, 1.0)
        self.append_node(root_path, name, arrow_node, frame)

    def append_mesh(self,
                    root_path: str,
                    name: str,
                    mesh_path: str,
                    scale: Optional[Tuple3FType] = None,
                    frame: Union[np.ndarray, Tuple[
                        Union[np.ndarray, Sequence[float]],
                        Union[np.ndarray, Sequence[float]]]] = None,
                    no_cache: Optional[bool] = None) -> None:
        """Append a mesh node to the group.

        :param root_path: Path to the group's root node
        :param name: Node name within a group
        :param mesh_path: Path to the mesh file on disk
        :param scale: Mesh scale.
                    Optional: No rescaling by default.
        :param frame: Local frame position and quaternion.
                      Optional: ((0., 0., 0.), (0., 0., 0., 0.)) by default.
        :param no_cache: Use cache to load a model.
                         Optional: may depend on the mesh file.
        """
        # Load the mesh
        mesh = self.loader.loadModel(mesh_path, noCache=no_cache)

        # Fix the orientation of the mesh if it has '.dae' extension
        if mesh_path.lower().endswith('.dae'):
            # Replace non-standard hard drive prefix on Windows
            if sys.platform.startswith('win'):
                mesh_path = re.sub(
                    r'^/([A-Za-z])', lambda m: m[1].upper() + ":", mesh_path)

            # Parse the mesh file toe extract axis up if provided
            def parse_xml(xml_path: str) -> Tuple[ET.Element, Dict[str, str]]:
                xml_iter = ET.iterparse(xml_path, events=["start-ns"])
                xml_namespaces = dict(prefix_namespace_pair
                                      for _, prefix_namespace_pair in xml_iter)
                return xml_iter.root, xml_namespaces

            root, ns = parse_xml(mesh_path)
            if ns:
                field_axis = root.find(f".//{{{ns['']}}}up_axis")
            else:
                field_axis = root.find(".//up_axis")

            # Change the orientation of the mesh if necessary
            if field_axis is not None:
                axis = field_axis.text.lower()
                if axis == 'z_up':
                    mesh.set_mat(Mat4.yToZUpMat())

        # Set the scale of the mesh
        if scale is not None:
            mesh.set_scale(*scale)

        # Render meshes two-sided in panda3d to avoid seeing through it
        mesh.set_two_sided(True)

        self.append_node(root_path, name, mesh, frame)

    def set_watermark(self,
                      img_fullpath: Optional[str] = None,
                      width: Optional[int] = None,
                      height: Optional[int] = None) -> None:
        # Remove existing watermark, if any
        if self._watermark is not None:
            self._watermark.remove_node()
            self._watermark = None

        # Do nothing if img_fullpath is not specified
        if img_fullpath is None or img_fullpath == "":
            return

        # Get image size if not user-specified
        if width is None or height is None:
            image_header = PNMImageHeader()
            image_header.readHeader(Filename(img_fullpath))
            width = width or float(image_header.getXSize())
            height = height or float(image_header.getYSize())

        # Compute relative image size
        width_win, height_win = self.getSize()
        width_rel, height_rel = width / width_win, height / height_win

        # Make sure it does not take too much space of window
        if width_rel > 0.2:
            width_rel, height_rel = 0.2, height_rel / width_rel * 0.2
        if height_rel > 0.2:
            width_rel, height_rel = width_rel / height_rel * 0.2, 0.2

        # Create image watermark on main window
        self._watermark = OnscreenImage(image=img_fullpath,
                                        parent=self.a2dBottomLeft,
                                        scale=(width_rel, 1, height_rel))

        # Add it on secondary window
        self.offA2dBottomLeft.node().add_child(self._watermark.node())

        # Move the watermark in bottom right corner
        self._watermark.set_pos(
            WIDGET_MARGIN_REL + width_rel, 0, WIDGET_MARGIN_REL + height_rel)

        if self.buff.inverted:
            self._legend.set_tex_scale(TextureStage.getDefault(), 1.0, -1.0)

    def set_legend(self,
                   items: Optional[Sequence[
                       Tuple[str, Optional[Sequence[int]]]]] = None) -> None:
        # Make sure plot submodule is available
        try:
            import matplotlib.pyplot as plt
            from matplotlib.patches import Patch
        except ImportError:
            raise ImportError(
                "Method not supported. Please install 'jiminy_py[plot]'.")

        # Remove existing watermark, if any
        if self._legend is not None:
            self._legend.remove_node()
            self._legend = None

        # Do nothing if items is not specified
        if items is None or not items:
            return

        # Switch to non-interactive backend to avoid hanging for some reason
        plt_backend = plt.get_backend()
        plt.switch_backend("Agg")

        # Create empty figure with the legend
        color_default = (0.0, 0.0, 0.0, 1.0)
        handles = [Patch(color=c or color_default, label=t) for t, c in items]
        fig, ax = plt.subplots()
        legend = ax.legend(handles=handles,
                           ncol=len(handles),
                           framealpha=1,
                           frameon=True)
        ax.set_axis_off()

        # Render the legend
        fig.draw(renderer=fig.canvas.get_renderer())

        # Compute bbox size to be power of 2 for software rendering.
        bbox = legend.get_window_extent().padded(2)
        bbox_inches = bbox.transformed(fig.dpi_scale_trans.inverted())
        bbox_pixels = np.array(bbox_inches.extents) * LEGEND_DPI
        bbox_pixels = np.floor(bbox_pixels)
        bbox_pixels[2:] = bbox_pixels[:2] + 2 ** np.ceil(np.log(
            bbox_pixels[2:] - bbox_pixels[:2]) / np.log(2.0)) + 0.1
        bbox_inches = bbox.from_extents(bbox_pixels / LEGEND_DPI)

        # Export the figure, limiting the bounding box to the legend area,
        # slightly extended to ensure the surrounding rounded corner box of
        # is not cropped. Transparency is enabled, so it is not an issue.
        io_buf = io.BytesIO()
        fig.savefig(io_buf, format='rgba', dpi=LEGEND_DPI, transparent=True,
                    bbox_inches=bbox_inches)
        io_buf.seek(0)
        img_raw = io_buf.getvalue()
        width, height = map(int, bbox_pixels[2:] - bbox_pixels[:2])

        # Delete the legend along with its temporary figure
        plt.close(fig)

        # Restore original backend
        plt.switch_backend(plt_backend)

        # Create texture in which to render the image buffer
        tex = Texture()
        tex.setup2dTexture(
            width, height, Texture.T_unsigned_byte, Texture.F_rgba8)
        tex.set_ram_image_as(img_raw, 'rgba')

        # Compute relative image size
        width_win, height_win = self.getSize()
        imgAspectRatio = width / height
        width_rel = LEGEND_SCALE * width / width_win
        height_rel = LEGEND_SCALE * height / height_win
        if height_rel * imgAspectRatio < width_rel:
            width_rel = height_rel * imgAspectRatio
        else:
            height_rel = width_rel / imgAspectRatio

        # Create legend on main window
        self._legend = OnscreenImage(image=tex,
                                     parent=self.a2dBottomCenter,
                                     scale=(width_rel, 1, height_rel))

        # Add it on secondary window
        self.offA2dBottomCenter.node().add_child(self._legend.node())

        # Move the legend in top left corner
        self._legend.set_pos(0, 0, WIDGET_MARGIN_REL + height_rel)

        # Flip the vertical axis and enable transparency
        self._legend.set_transparency(TransparencyAttrib.MAlpha)
        if self.buff.inverted:
            self._legend.set_tex_scale(TextureStage.getDefault(), 1.0, -1.0)

    def set_clock(self, time: Optional[float] = None) -> None:
        # Make sure plot submodule is available
        try:
            from matplotlib import font_manager
        except ImportError:
            raise ImportError(
                "Method not supported. Please install 'jiminy_py[plot]'.")

        # Remove existing watermark, if any
        if time is None:
            if self._clock is not None:
                self._clock.remove_node()
                self._clock = None
            return

        if self._clock is None:
            # Get path of default matplotlib font
            fontpath = _sanitize_path(font_manager.findfont(None))

            # Create clock on main window.
            self._clock = OnscreenText(
                text="00:00:00.000",
                parent=self.a2dBottomRight,
                scale=CLOCK_SCALE,
                font=self.loader.loadFont(fontpath),
                fg=(1, 0, 0, 1),
                bg=(1, 1, 1, 1),
                frame=(0, 0, 0, 1),
                mayChange=True,
                align=TextNode.ARight)

            # Add it on secondary window
            self.offA2dBottomRight.node().add_child(self._clock.node())

            # Fix card margins not uniform
            self._clock.textNode.set_card_as_margin(0.2, 0.2, 0.05, 0)
            self._clock.textNode.set_frame_as_margin(0.2, 0.2, 0.05, 0)

            # Move the clock in bottom right corner
            card_dims = self._clock.textNode.get_card_transformed()
            self._clock.set_pos(-WIDGET_MARGIN_REL-card_dims[1],
                                0,
                                WIDGET_MARGIN_REL-card_dims[2])

        # Update clock values
        hours, remainder = divmod(time, 3600)
        minutes, seconds = divmod(remainder, 60)
        remainder, seconds = math.modf(seconds)
        milliseconds = 1000 * remainder
        self._clock.setText(f"{hours:02.0f}:{minutes:02.0f}:{seconds:02.0f}"
                            f".{milliseconds:03.0f}")

    def set_material(self,
                     root_path: str,
                     name: str,
                     color: Optional[Tuple4FType] = None,
                     texture_path: str = '',
                     disable_material: bool = False) -> None:
        """Patched to avoid raising an exception if node does not exist, and to
        clear color if not specified. In addition, texture are disabled if the
        color is specified, and a physics-based shader is used if available.
        """
        node = self._groups[root_path].find(name)
        if node:
            if disable_material:
                node.set_texture_off(1)
            else:
                node.clear_texture()
                node.clear_material()

            if color is None:
                node.clear_color()
            else:
                node.set_color(Vec4(*color))

                material = Material()
                material.set_ambient(Vec4(*color))
                material.set_diffuse(Vec4(*color))
                material.set_specular(Vec3(1, 1, 1))
                material.set_roughness(0.4)
                node.set_material(material, 1)

                if color[3] < 1:
                    node.set_transparency(TransparencyAttrib.M_alpha)
                else:
                    node.set_transparency(TransparencyAttrib.M_none)

            if texture_path:
                texture = self.loader.load_texture(texture_path)
                node.set_texture(texture)

    def set_scale(self,
                  root_path: str,
                  name: str,
                  scale: Optional[Tuple3FType] = None) -> None:
        """Override scale of node of a node.
        """
        node = self._groups[root_path].find(name)
        if node:
            if any(abs(s) < 1e-3 for s in scale):
                if not node.is_hidden():
                    node.set_tag("status", "disable")
                    node.hide()
            else:
                node.set_scale(*scale)
                if node.get_tag("status") == "disable":
                    node.set_tag("status", "auto")
                    node.show()

    def set_scales(self, root_path, name_scales_dict):
        """Override scale of nodes within a group.
        """
        for name, scale in name_scales_dict.items():
            self.set_scale(root_path, name, scale)

    def move_node(self,
                  root_path: str,
                  name: str,
                  frame: FrameType) -> None:
        """Set pose of a single node.
        """
        node = self._groups[root_path].find(name)
        if isinstance(frame, np.ndarray):
            node.set_mat(Mat4(*frame.T.flat))
        else:
            pos, quat = frame
            node.set_pos_quat(Vec3(*pos), Quat(*quat))

    def remove_node(self, root_path: str, name: str) -> None:
        """Remove a single node from the scene.
        """
        node = self._groups[root_path].find(name)
        if node:
            node.remove_node()

    def show_node(self,
                  root_path: str,
                  name: str,
                  show: bool,
                  always_foreground: Optional[bool] = None) -> None:
        """Turn rendering on or off for a single node.
        """
        node = self._groups[root_path].find(name)
        if node:
            if show:
                if node.get_tag("status") in ("hidden", ""):
                    node.set_tag("status", "auto")
                    node.show()
            else:
                node.set_tag("status", "hidden")
                node.hide()
            if always_foreground is not None:
                if always_foreground:
                    node.set_bin("fixed", 0)
                else:
                    node.clear_bin()
                node.set_depth_test(not always_foreground)
                node.set_depth_write(not always_foreground)

    def get_camera_transform(self) -> Tuple[np.ndarray, np.ndarray]:
        return (np.array(self.camera.get_pos()),
                np.array(self.camera.get_quat()))

    def set_camera_transform(self,
                             pos: Tuple3FType,
                             quat: np.ndarray,
                             lookat: Tuple3FType = (0.0, 0.0, 0.0)) -> None:
        self.camera.set_pos(*pos)
        self.camera.set_quat(LQuaternion(quat[-1], *quat[:-1]))
        self.camera_lookat = np.array(lookat)

    def set_camera_lookat(self,
                          pos: Tuple3FType) -> None:
        self.camera.set_pos(
            self.camera.get_pos() + Vec3(*pos) - Vec3(*self.camera_lookat))
        self.camera_lookat = np.asarray(pos)

    def set_window_size(self, width: int, height: int) -> None:
        self.buff.set_size(width, height)
        self._adjust_offscreen_window_aspect_ratio()

    def set_framerate(self, framerate: Optional[float] = None) -> None:
        """Limit framerate of Panda3d to avoid consuming too much ressources.

        :param framerate: Desired framerate limit. None to disable.
                          Optional: Disable framerate limit by default.
        """
        if framerate is not None:
            self.clock.set_mode(ClockObject.MLimited)
            self.clock.set_frame_rate(PANDA3D_FRAMERATE_MAX)
        else:
            self.clock.set_mode(ClockObject.MNormal)
        self.framerate = framerate

    def get_framerate(self) -> Optional[float]:
        """Get current framerate limit.
        """
        return self.framerate

    def save_screenshot(self, filename: Optional[str] = None) -> bool:
        # Generate filename based on current time if not provided
        if filename is None:
            template = 'screenshot-%Y-%m-%d-%H-%M-%S.png'
            filename = datetime.now().strftime(template)

        # Refresh the scene to make sure it is perfectly up-to-date.
        # It will take into account the updated position of the camera.
        self.graphics_engine.render_frame()

        # Capture frame as image
        image = PNMImage()
        if not self.buff.get_screenshot(image):
            return False

        # Flip the image if the buffer is also flipped to revert the effect
        if self.buff.inverted:
            image.flip(flip_x=False, flip_y=True, transpose=False)

        # Remove alpha if format does not support it
        if not filename.lower().endswith('.png'):
            image.remove_alpha()

        # Save the image
        if not image.write(filename):
            return False

        return True

    def get_screenshot(self,
                       requested_format: str = 'RGB',
                       raw: bool = False) -> Union[np.ndarray, bytes]:
        """Patched to take screenshot of the last window available instead of
        the main one, and to add raw data return mode for efficient
        multiprocessing.

        .. warning::
            Note that the speed of this method is limited by the global
            framerate, as any other method relaying on low-level panda3d task
            scheduler. The framerate limit must be disable manually to avoid
            such limitation.

        .. note::
            Internally, Panda3d uses BGRA, so using it is slightly faster than
            RGBA, but not RGB since there is one channel missing.

        :param requested_format: Desired export format (e.g. 'RGB' or 'BGRA')
        :param raw: whether to return a raw memory view of bytes, of a
                    structured `np.ndarray` of uint8 with dimensions [H, W, D].
        """
        # Refresh the scene
        self.graphics_engine.render_frame()

        # Get frame as raw texture
        texture = self.buff.get_texture()

        # Extract raw array buffer from texture
        image = texture.get_ram_image_as(requested_format)

        # Return raw buffer if requested
        if raw:
            return image.get_data()

        # Convert raw texture to numpy array if requested
        xsize, ysize = texture.get_x_size(), texture.get_y_size()
        return np.frombuffer(image, np.uint8).reshape((ysize, xsize, -1))

    def enable_shadow(self, enable: bool) -> None:
        for light in self._lights:
            if not light.node().is_ambient_light():
                light.node().set_shadow_caster(enable)
        self.render.set_depth_offset(-2 if enable else 0)
        self._shadow_enabled = enable

panda3d_viewer.viewer_app.ViewerApp = Panda3dApp  # noqa


class Panda3dProxy(panda3d_viewer.viewer_proxy.ViewerAppProxy):
    def __getstate__(self) -> dict:
        """Required for Windows and OS X support, which use spawning instead of
        forking to create subprocesses, requiring pickling of process instance.
        """
        return vars(self)

    def __setstate__(self, state: dict) -> None:
        """Defined for the same reason than `__getstate__`.
        """
        vars(self).update(state)

    def __getattr__(self, name: str) -> Callable:
        """Patched to avoid deadlock when closing window.
        """
        def _send(*args, **kwargs):
            if self._host_conn.closed:
                raise ViewerClosedError('User closed the main window')
            self._host_conn.send((name, args, kwargs))
            try:
                reply = self._host_conn.recv()
            except EOFError:
                # This exception may arise if the last command was interrupted
                reply = self._host_conn.recv()
            if isinstance(reply, Exception):
                if isinstance(reply, ViewerClosedError):
                    # Close pipe to make sure it does not get used in future
                    self._host_conn.close()
                raise reply
            return reply

        return _send

    def run(self) -> None:
        """Patched to use Jiminy ViewerApp instead of the original one.
        """
        panda3d_viewer.viewer_app.ViewerApp = Panda3dApp  # noqa
        return super().run()

panda3d_viewer.viewer_proxy.ViewerAppProxy = Panda3dProxy  # noqa


class Panda3dViewer(panda3d_viewer.viewer.Viewer):
    def __getattr__(self, name: str) -> Any:
        return getattr(self.__getattribute__('_app'), name)

    def __dir__(self) -> List[str]:
        return super().__dir__() + self._app.__dir__()

    def set_material(self, *args: Any, **kwargs: Any) -> None:
        self._app.set_material(*args, **kwargs)

    def append_frame(self, *args: Any, **kwargs: Any) -> None:
        self._app.append_frame(*args, **kwargs)

    def append_cylinder(self, *args: Any, **kwargs: Any) -> None:
        self._app.append_cylinder(*args, **kwargs)

    def save_screenshot(self, filename: Optional[str] = None):
        return self._app.save_screenshot(filename)

    def get_screenshot(self,
                       requested_format: str = 'RGBA',
                       raw: bool = False) -> Union[np.ndarray, bytes]:
        return self._app.get_screenshot(requested_format, raw)

panda3d_viewer.viewer.Viewer = Panda3dViewer  # noqa


class Panda3dVisualizer(BaseVisualizer):
    """A Pinocchio display using panda3d engine.

    Based on https://github.com/stack-of-tasks/pinocchio/blob/master/bindings/python/pinocchio/visualize/panda3d_visualizer.py
    Copyright (c) 2014-2020, CNRS
    Copyright (c) 2018-2020, INRIA
    """  # noqa: E501
    def initViewer(self,
                   viewer: Optional[Union[Panda3dViewer, Panda3dApp]] = None,
                   loadModel: bool = False,
                   **kwargs: Any) -> None:
        """Init the viewer by attaching to / creating a GUI viewer.
        """
        self.visual_group = None
        self.collision_group = None
        self.display_visuals = False
        self.display_collisions = False
        self.viewer = viewer

        if viewer is None:
            self.viewer = Panda3dViewer(window_title="jiminy")

        if loadModel:
            self.loadViewerModel(rootNodeName=self.model.name)

    def getViewerNodeName(self,
                          geometry_object: hppfcl.CollisionGeometry,
                          geometry_type: pin.GeometryType) -> Tuple[str, str]:
        """Return the name of the geometry object inside the viewer.
        """
        if geometry_type is pin.GeometryType.VISUAL:
            return self.visual_group, geometry_object.name
        elif geometry_type is pin.GeometryType.COLLISION:
            return self.collision_group, geometry_object.name

    def loadViewerGeometryObject(self,
                                 geometry_object: hppfcl.CollisionGeometry,
                                 geometry_type: pin.GeometryType,
                                 color: Optional[np.ndarray] = None) -> None:
        """Load a single geometry object
        """
        # Skip ground plane
        if geometry_object.name == "ground":
            return

        # Get node name
        node_name = self.getViewerNodeName(geometry_object, geometry_type)

        # Extract geometry information
        geom = geometry_object.geometry
        mesh_path = geometry_object.meshPath
        texture_path = ""
        if geometry_object.overrideMaterial:
            # Get material from URDF. The color is only used if no texture or
            # if its value is not the default because meshColor is never unset.
            if os.path.exists(geometry_object.meshTexturePath):
                texture_path = geometry_object.meshTexturePath
            if color is None and (not texture_path or any(
                    geometry_object.meshColor != [0.9, 0.9, 0.9, 1.0])):
                color = geometry_object.meshColor

        # Try to load mesh from path first, to take advantage of very effective
        # Panda3d model caching procedure.
        is_success = True
        if os.path.exists(mesh_path):
            # append a mesh
            mesh_path = _sanitize_path(geometry_object.meshPath)
            scale = npToTuple(geometry_object.meshScale)
            self.viewer.append_mesh(*node_name, mesh_path, scale)
        else:
            # Each geometry must have at least a color or a texture
            if color is None and not texture_path:
                color = np.array([0.75, 0.75, 0.75, 1.0])

            # Append a primitive geometry
            if isinstance(geom, hppfcl.Capsule):
                self.viewer.append_capsule(
                    *node_name, geom.radius, 2.0 * geom.halfLength)
            elif isinstance(geom, hppfcl.Cylinder):
                self.viewer.append_cylinder(
                    *node_name, geom.radius, 2.0 * geom.halfLength)
            elif isinstance(geom, hppfcl.Cone):
                self.viewer.append_cone(
                    *node_name, geom.radius, 2.0 * geom.halfLength)
            elif isinstance(geom, hppfcl.Box):
                self.viewer.append_box(*node_name, 2.0 * geom.halfSide)
            elif isinstance(geom, hppfcl.Sphere):
                self.viewer.append_sphere(*node_name, geom.radius)
            elif isinstance(geom, (hppfcl.Convex, hppfcl.BVHModelBase)):
                # Extract vertices and faces from geometry
                if isinstance(geom, hppfcl.Convex):
                    num_vertices = geom.num_points
                    get_vertices = geom.points
                    num_faces = geom.num_polygons
                    get_faces = geom.polygons
                else:
                    num_vertices = geom.num_vertices
                    get_vertices = geom.vertices
                    num_faces, get_faces = geom.num_tris, geom.tri_indices
                vertices = np.empty((num_vertices, 3))
                for i in range(num_vertices):
                    vertices[i] = get_vertices(i)
                faces = np.empty((num_faces, 3), dtype=np.int32)
                for i in range(num_faces):
                    tri = get_faces(i)
                    for j in range(3):
                        faces[i, j] = tri[j]

                # Return immediately if there is nothing to load
                if num_vertices == 0:
                    return

                # Create primitive triangle geometry.
                # Note that the redundant vertices are added for efficiency
                # because avoiding duplication requires expensive search.
                vformat = GeomVertexFormat.get_v3n3()
                vdata = GeomVertexData('vdata', vformat, Geom.UHStatic)
                vdata.unclean_set_num_rows(3 * len(faces))
                vwriter = GeomVertexWriter(vdata, 'vertex')
                nwriter = GeomVertexWriter(vdata, "normal")
                normals = np.empty((len(faces), 3))
                for i, face in enumerate(faces):
                    pts = vertices[face]
                    v1, v2 = pts[1] - pts[0], pts[2] - pts[1]
                    normals[i] = np.cross(v1, v2)
                    for pt in pts:
                        vwriter.add_data3(*pt)
                normals /= np.linalg.norm(normals, axis=0)
                for normal in normals:
                    for i in range(3):
                        nwriter.add_data3(*normal)
                prim = GeomTriangles(Geom.UHStatic)
                prim.reserveNumVertices(len(faces))
                faces.flat[:] = np.arange(faces.size)
                for face in faces:
                    prim.addVertices(*face)
                obj = Geom(vdata)
                obj.addPrimitive(prim)

                # Add the primitive geometry to the scene
                geom_node = GeomNode(geometry_object.name)
                geom_node.add_geom(obj)
                node = NodePath(geom_node)
                node.set_two_sided(True)
                self.viewer.append_node(*node_name, node)
            else:
                is_success = False

        # Early return if impossible to load the geometry for some reason
        if not is_success:
            warnings.warn(
                f"Unsupported geometry type for {geometry_object.name} "
                f"({type(geom)})", category=UserWarning, stacklevel=2)
            return

        # Set material
        self.viewer.set_material(*node_name, color, texture_path)

    def loadViewerModel(self,
                        rootNodeName: str,
                        color: Optional[np.ndarray] = None) -> None:
        """Create a group of nodes displaying the robot meshes in the viewer.
        """
        self.root_name = rootNodeName

        # Load robot visual meshes
        self.visual_group = "/".join((self.root_name, "visuals"))
        self.viewer.append_group(self.visual_group)
        for visual in self.visual_model.geometryObjects:
            self.loadViewerGeometryObject(
                visual, pin.GeometryType.VISUAL, color)
        self.displayVisuals(True)

        # Load robot collision meshes
        self.collision_group = "/".join((self.root_name, "collisions"))
        self.viewer.append_group(self.collision_group)
        for collision in self.collision_model.geometryObjects:
            self.loadViewerGeometryObject(
                collision, pin.GeometryType.COLLISION, color)
        self.displayCollisions(False)

    def display(self, q: np.ndarray) -> None:
        """Display the robot at configuration q in the viewer by placing all
        the bodies."""
        pin.forwardKinematics(self.model, self.data, q)

        def move(group, model, data):
            pin.updateGeometryPlacements(self.model, self.data, model, data)
            name_pose_dict = {}
            for obj in model.geometryObjects:
                oMg = data.oMg[model.getGeometryId(obj.name)]
                x, y, z, qx, qy, qz, qw = pin.SE3ToXYZQUAT(oMg)
                name_pose_dict[obj.name] = ((x, y, z), (qw, qx, qy, qz))
            self.viewer.move_nodes(group, name_pose_dict)

        if self.display_visuals:
            move(self.visual_group, self.visual_model, self.visual_data)

        if self.display_collisions:
            move(self.collision_group, self.collision_model,
                 self.collision_data)

    def displayCollisions(self, visibility: bool) -> None:
        """Set whether to display collision objects or not."""
        self.viewer.show_group(self.collision_group, visibility)
        self.display_collisions = visibility

    def displayVisuals(self, visibility: bool) -> None:
        """Set whether to display visual objects or not."""
        self.viewer.show_group(self.visual_group, visibility)
        self.display_visuals = visibility
