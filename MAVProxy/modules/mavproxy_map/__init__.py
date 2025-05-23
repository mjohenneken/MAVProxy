#!/usr/bin/env python3
'''
map display module
Andrew Tridgell
June 2012
'''

import sys, os, math
import functools
import time
import datetime
from MAVProxy.modules.lib import mp_util
from MAVProxy.modules.lib import mp_settings
from MAVProxy.modules.lib import mp_module
from MAVProxy.modules.lib.mp_menu import *
from pymavlink import mavutil
from PIL import ImageColor

class MapModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super(MapModule, self).__init__(mpstate, "map", "map display", public = True, multi_instance=True, multi_vehicle=True)
        cmdname = "map"
        if self.instance > 1:
            cmdname += "%u" % self.instance
        # lat/lon per system ID
        self.lat_lon_heading = {}
        self.wp_change_time = 0
        self.fence_change_time = 0
        self.rally_change_time = 0
        self.terrain_contour_ids = []
        self.have_simstate = False
        self.have_vehicle = {}
        self.move_wp = -1
        self.moving_wp = None
        self.moving_fencepoint = None
        self.moving_rally = None
        self.mission_list = None
        self.moving_polygon_point = None
        self.moving_circle = None
        self.setting_circle_radius = None
        self.icon_counter = 0
        self.circle_counter = 0
        self.draw_line = None
        self.draw_callback = None
        self.current_ROI = None
        self.have_global_position = False
        self.vehicle_type_by_sysid = {}
        self.vehicle_type_name = 'plane'
        self.last_unload_check_time = time.time()
        self.unload_check_interval = 0.1 # seconds
        self.trajectory_layers = set()
        self.vehicle_type_override = {}
        self.map_settings = mp_settings.MPSettings(
            [ ('showgpspos', int, 1),
              ('showgps2pos', int, 1),
              ('showsimpos', int, 0),
              ('showahrspos', int, 1),
              ('showahrs2pos', int, 0),
              ('showahrs3pos', int, 0),
              ('brightness', float, 1),
              ('rallycircle', bool, False),
              ('loitercircle',bool, False),
              ('showclicktime',int, 2),
              ('showwpnum',bool, True),
              ('circle_linewidth', int, 1),
              ('showdirection', bool, False),
              ('setpos_accuracy', float, 50),
              ('mission_color', str, "white"),
              ('font_size', float, 0.5),
              ('contour_levels', int, 20),
              ('contour_grid_spacing', float, 30.0),
              ('contour_grid_extent', float, 20000.0),
            ])
        
        service='MicrosoftHyb'
        if 'MAP_SERVICE' in os.environ:
            service = os.environ['MAP_SERVICE']
        import platform
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        title = "Map"
        if self.instance > 1:
            title += str(self.instance)
        elevation = None
        terrain_module = self.module('terrain')
        if terrain_module is not None:
            elevation = terrain_module.ElevationModel.database
        self.map = mp_slipmap.MPSlipMap(service=service, elevation=elevation, title=title)
        if self.instance == 1:
            self.mpstate.map = self.map
            mpstate.map_functions = { 'draw_lines' : self.draw_lines }

        self.map.add_callback(functools.partial(self.map_callback))
        self.add_command(cmdname, self.cmd_map, "map control", ['icon',
                                                                'set (MAPSETTING)',
                                                                'vehicletype',
                                                                'zoom',
                                                                'center',
                                                                'follow',
                                                                'menu',
                                                                'marker',
                                                                'clear'])
        self.add_completion_function('(MAPSETTING)', self.map_settings.completion)

        self.default_popup = MPMenuSubMenu('Popup', items=[])
        self.add_menu(MPMenuItem('Fly To', 'Fly To', '# guided ',
                                 handler=MPMenuCallTextDialog(title='Altitude (FLYTOFRAMEUNITS)', default=self.mpstate.settings.guidedalt,
                                                              settings=self.settings)))
        self.add_menu(MPMenuItem('Terrain Check', 'Terrain Check', '# terrain check'))
        self.add_menu(MPMenuItem('Show Position', 'Show Position', 'showPosition'))
        self.add_menu(MPMenuItem('Google Maps Link', 'Google Maps Link', 'printGoogleMapsLink'))
        self.add_menu(MPMenuItem('Set ROI', 'Set ROI', '# map setroi '))
        self.add_menu(MPMenuItem('Set Position', 'Set Position', '# map setposition '))
        self.add_menu(MPMenuSubMenu('Home', items=[
            MPMenuItem('Set Home', 'Set Home', '# confirm "Set HOME?" map sethomepos '),
            MPMenuItem('Set Home (with height)', 'Set Home', '# confirm "Set HOME with height?" map sethome '),
            MPMenuItem('Set Origin', 'Set Origin', '# confirm "Set ORIGIN?" map setoriginpos '),
            MPMenuItem('Set Origin (with height)', 'Set Origin', '# confirm "Set ORIGIN with height?" map setorigin '),
        ]))

        self.cmd_menu_add(["Marker:Flag", "map", "marker", "flag"])
        self.cmd_menu_add(["Marker:Barrell", "map", "marker", "barrell"])
        self.cmd_menu_add(["Marker:Flame", "map", "marker", "flame"])

        self._colour_for_wp_command = {
            # takeoff commands
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF: (255,0,0),
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF_LOCAL: (255,0,0),
            mavutil.mavlink.MAV_CMD_NAV_VTOL_TAKEOFF: (255,0,0),

            # land commands
            mavutil.mavlink.MAV_CMD_NAV_LAND_LOCAL: (255,255,0),
            mavutil.mavlink.MAV_CMD_NAV_LAND: (255,255,0),
            mavutil.mavlink.MAV_CMD_NAV_VTOL_LAND: (255,255,0),

            # waypoint commands
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT: (0,255,255),
            mavutil.mavlink.MAV_CMD_NAV_SPLINE_WAYPOINT: (64,255,64),

            # other commands
            mavutil.mavlink.MAV_CMD_DO_LAND_START: (255,127,0),
        }
        self._label_suffix_for_wp_command = {
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF: "TOff",
            mavutil.mavlink.MAV_CMD_DO_LAND_START: "DLS",
            mavutil.mavlink.MAV_CMD_NAV_SPLINE_WAYPOINT: "SW",
            mavutil.mavlink.MAV_CMD_NAV_VTOL_LAND: "VL",
        }

        self.add_menu(MPMenuSubMenu('Terrain', items=[
            MPMenuItem('Show Contours', returnkey='showTerrainContours'),
            MPMenuItem('Hide Contours', returnkey='hideTerrainContours'),
            MPMenuItem('Remove Contours', returnkey='removeTerrainContours'),
        ]))

    def add_menu(self, menu):
        '''add to the default popup menu'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        self.default_popup.add(menu)
        self.map.add_object(mp_slipmap.SlipDefaultPopup(self.default_popup, combine=True))

    def cmd_menu_add(self, args):
        '''add to map menus'''
        if len(args) < 2:
            print("Usage: map menu add MenuPath command")
            return
        menupath = args[0].strip('"').split(':')
        name = menupath[-1]
        cmd = '# ' + ' '.join(args[1:])
        self.default_popup.add_to_submenu(menupath[:-1], MPMenuItem(name, name, cmd))
        self.map.add_object(mp_slipmap.SlipDefaultPopup(self.default_popup, combine=True))

    def cmd_menu(self, args):
        '''control console menus'''
        if len(args) < 2:
            print("Usage: map menu <add>")
            return
        if args[0] == 'add':
            self.cmd_menu_add(args[1:])
        
    def remove_menu(self, menu):
        '''add to the default popup menu'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        self.default_popup.remove(menu)
        self.map.add_object(mp_slipmap.SlipDefaultPopup(self.default_popup, combine=True))

    def show_position(self):
        '''show map position click information'''
        pos = self.mpstate.click_location
        dms = (mp_util.degrees_to_dms(pos[0]), mp_util.degrees_to_dms(pos[1]))
        msg =  "Coordinates in WGS84\n"
        msg += "Decimal: %.6f %.6f\n" % (pos[0], pos[1])
        msg += "DMS:     %s %s\n" % (dms[0], dms[1])
        msg += "Grid:    %s\n" % mp_util.latlon_to_grid(pos)
        if self.logdir:
            logf = open(os.path.join(self.logdir, "positions.txt"), "a")
            logf.write("Position: %.6f %.6f at %s\n" % (pos[0], pos[1], time.ctime()))
            logf.close()
        posbox = MPMenuChildMessageDialog('Position', msg, font_size=32)
        posbox.show()

    def print_google_maps_link(self):
        '''show map position click information'''
        pos = self.mpstate.click_location
        print("https://www.google.com/maps/search/?api=1&query=%f,%f" % (pos[0], pos[1]))

    def write_JSON(self, fname, template, vardict):
        '''write a JSON file in log directory'''
        fname = os.path.join(self.logdir, fname)
        json = template
        for k in vardict.keys():
            value = vardict[k]
            json = json.replace("{{" + k + "}}", str(value))
        open(fname, 'w').write(json)

    def cmd_map_marker(self, args, latlon=None):
        '''add a map marker'''
        usage = "Usage: map marker <icon>"
        if latlon is None:
            latlon = self.mpstate.click_location
        if latlon is None:
            print("Need click position for marker")
            return
        (lat, lon) = latlon
        marker = 'flag'
        text = ''
        if len(args) > 0:
            marker = args[0]

        if len(args) > 1:
            text = ' '.join(args[1:])
        flag = marker + '.png'

        icon = self.map.icon(flag)
        self.map.add_object(mp_slipmap.SlipIcon(
            'icon - %s [%u]' % (str(flag),self.icon_counter),
            (float(lat),float(lon)),
            icon, layer=3, rotation=0, follow=False))
        self.icon_counter += 1
        now = time.time()
        tstr = datetime.datetime.fromtimestamp(now).strftime("%Y_%m_%d_%H_%M_%S")
        subsec = now - math.floor(now)
        millis = int(subsec * 1000)
        fname = "marker_%s_%03u.json" % (tstr, millis)

        gpi = self.master.messages.get('GLOBAL_POSITION_INT',None)
        att = self.master.messages.get('ATTITUDE',None)

        self.write_JSON(fname,'''{
"marker" : {{MARKER}},
"text" : "{{TEXT}}",
"tsec" : {{TIMESEC}},
"mlat" : {{LAT}},
"mlon" : {{LON}},
"vlat" : {{VLAT}},
"vlon" : {{VLON}},
"valt" : {{VALT}},
"gspeed" : {{GSPEED}},
"ghead" : {{GHEAD}},
"roll" : {{ROLL}},
"pitch" : {{PITCH}},
"yaw" : {{YAW}},
}
''', {
    "TIMESEC" : now,
    "MARKER" : marker,
    "TEXT" : text,
    "LAT" : lat,
    "LON" : lon,
    "VLAT" : "%.9f" % (gpi.lat*1.0e-7),
    "VLON" : "%.9f" % (gpi.lon*1.0e-7),
    "VALT" : gpi.alt*1.0e-3,
    "GSPEED" : math.sqrt(gpi.vx**2+gpi.vy**2)*0.01,
    "GHEAD" : gpi.hdg*0.01,
    "ROLL" : math.degrees(att.roll),
    "PITCH" : math.degrees(att.pitch),
    "YAW" : math.degrees(att.yaw)
    })

        print("Wrote marker %s" % fname)

    def cmd_map(self, args):
        '''map commands'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if len(args) < 1:
            print("usage: map <icon|set|menu|marker>")
        elif args[0] == "menu":
            self.cmd_menu(args[1:])
        elif args[0] == "icon":
            usage = "Usage: map icon <lat> <lon> <icon>"
            flag = 'flag.png'
            if len(args) > 2:
                lat = args[1]
                lon = args[2]
                if len(args) > 3:
                    flag = args[3] + '.png'
            elif self.mpstate.click_location is not None:
                if len(args) >= 1:
                    # i.e. "map icon"
                    (lat, lon) = self.mpstate.click_location
                    if len(args) == 2:
                        # i.e. map icon barrell
                        flag = args[1]
            else:
                print(usage)
                return

            icon = self.map.icon(flag)
            self.map.add_object(mp_slipmap.SlipIcon(
                'icon - %s [%u]' % (str(flag),self.icon_counter),
                (float(lat),float(lon)),
                icon, layer=3, rotation=0, follow=False))
            self.icon_counter += 1

        elif args[0] == "marker":
            self.cmd_map_marker(args[1:])
        elif args[0] == "vehicletype":
            if len(args) < 3:
                print("Usage: map vehicletype SYSID TYPE")
            else:
                sysid = int(args[1])
                vtype = int(args[2])
                self.vehicle_type_override[sysid] = vtype
                print("Set sysid %u to vehicle type %u" % (sysid, vtype))
        elif args[0] == "circle":
            self.cmd_map_circle(args[1:])
        elif args[0] == "set":
            self.map_settings.command(args[1:])
            self.map.add_object(mp_slipmap.SlipBrightness(self.map_settings.brightness))
        elif args[0] == "sethome":
            self.cmd_set_home(args)
        elif args[0] == "sethomepos":
            self.cmd_set_homepos(args)
        elif args[0] == "setorigin":
            self.cmd_set_origin(args)
        elif args[0] == "setoriginpos":
            self.cmd_set_originpos(args)
        elif args[0] == "zoom":
            self.cmd_zoom(args)
        elif args[0] == "center":
            self.cmd_center(args)
        elif args[0] == "follow":
            self.cmd_follow(args)
        elif args[0] == "clear":
            self.cmd_clear(args)
        elif args[0] == "setroi":
            self.cmd_set_roi(args)
        elif args[0] == "setposition":
            self.cmd_set_position(args)
        else:
            print("usage: map <icon|set>")

    def cmd_map_circle(self, args):
        usage = '''
Usage: map circle <lat> <lon> <radius> <colour>
Usage: map circle <radius> <colour>
        '''

        lat = None
        colour = None
        # syntax 1, lat/lon/radius/colour
        if len(args) == 4:
            colour = args[3]
            args = args[0:3]
        if len(args) == 3:
            lat = args[0]
            lon = args[1]
            radius = args[2]

        # syntax 2, radius/colour, uses click position
        if len(args) == 2:
            colour = args[1]
            args = args[0:1]
        if len(args) == 1:
            pos = self.mpstate.click_location
            if pos is None:
                print("Need click or location")
                print(usage)
                return

            (lat, lon) = pos
            radius = args[0]

        if lat is None:
            print(usage)
            return

        if colour is None:
            colour = "red"

        if colour == "red":
            colour = (255,0,0)
        elif colour == "green":
            colour = (0,255,0)
        elif colour == "blue":
            colour = (0,0,255)
        else:
            colour = eval(colour)

        circle = mp_slipmap.SlipCircle(
            "circle %u" % self.circle_counter,
            3,
            (float(lat), float(lon)),
            float(radius),
            colour,
            linewidth=self.map_settings.circle_linewidth,
        )
        self.map.add_object(circle)
        self.circle_counter += 1

    def colour_for_wp(self, wp_num):
        '''return a tuple describing the colour a waypoint should appear on the map'''
        wp = self.module('wp').wploader.wp(wp_num)
        command = wp.command
        return self._colour_for_wp_command.get(command, (0,255,0))

    def label_for_waypoint(self, wp_num):
        '''return the label the waypoint which should appear on the map'''
        wp = self.module('wp').wploader.wp(wp_num)
        command = wp.command
        if command not in self._label_suffix_for_wp_command:
            return str(wp_num)
        return str(wp_num) + "(" + self._label_suffix_for_wp_command[command] + ")"

    def display_waypoints(self):
        '''display the waypoints'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        self.mission_list = self.module('wp').wploader.view_list()
        polygons = self.module('wp').wploader.polygon_list()
        self.map.add_object(mp_slipmap.SlipClearLayer('Mission'))
        items = [MPMenuItem('WP Set', returnkey='popupMissionSet'),
                     MPMenuItem('WP Remove', returnkey='popupMissionRemove'),
                     MPMenuItem('WP Move', returnkey='popupMissionMove'),
                     MPMenuItem('WP Split', returnkey='popupMissionSplit'),
                    ]
        for i in range(len(polygons)):
            p = polygons[i]
            if len(p) > 1:
                popup = MPMenuSubMenu('Popup', items)
                self.map.add_object(mp_slipmap.SlipPolygon('mission %u' % i, p,
                                                                   layer='Mission', linewidth=2, colour=ImageColor.getrgb(self.map_settings.mission_color),
                                                                   arrow = self.map_settings.showdirection, popup_menu=popup))
        labeled_wps = {}
        self.map.add_object(mp_slipmap.SlipClearLayer('LoiterCircles'))
        if not self.map_settings.showwpnum:
            return
        font_size = self.map_settings.font_size
        for i in range(len(self.mission_list)):
            next_list = self.mission_list[i]
            for j in range(len(next_list)):
                #label already printed for this wp?
                if (next_list[j] not in labeled_wps):
                    label = self.label_for_waypoint(next_list[j])
                    colour = self.colour_for_wp(next_list[j])
                    self.map.add_object(mp_slipmap.SlipLabel(
                        'miss_cmd %u/%u' % (i,j), polygons[i][j], label, 'Mission', colour=colour, size=font_size))

                    if (self.map_settings.loitercircle and
                        self.module('wp').wploader.wp_is_loiter(next_list[j])):
                        wp = self.module('wp').wploader.wp(next_list[j])                    
                        if wp.command != mavutil.mavlink.MAV_CMD_NAV_LOITER_TO_ALT and wp.param3 != 0:
                            # wp radius and direction is defined by the mission
                            loiter_rad = wp.param3
                        elif wp.command == mavutil.mavlink.MAV_CMD_NAV_LOITER_TO_ALT and wp.param2 != 0:
                            # wp radius and direction is defined by the mission
                            loiter_rad = wp.param2
                        else:
                            # wp radius and direction is defined by the parameter
                            loiter_rad = self.get_mav_param('WP_LOITER_RAD')
                            
                        self.map.add_object(mp_slipmap.SlipCircle('Loiter Circle %u' % (next_list[j] + 1), 'LoiterCircles', polygons[i][j],
                                                                          loiter_rad, (255, 255, 255), 2, arrow = self.map_settings.showdirection))

                    labeled_wps[next_list[j]] = (i,j)

    # Start: handling of PolyFence popup menu items
    def polyfence_remove_circle(self, id):
        '''called when a fence is right-clicked and remove is selected;
        removes the circle
        '''
        (seq, type) = id.split(":")
        self.module('fence').removecircle(int(seq))

    def polyfence_move_circle(self, id):
        '''called when a fence is right-clicked and move circle is selected; start
        moving the circle
        '''
        (seq, t) = id.split(":")
        self.moving_circle = int(seq)

    def polyfence_set_circle_radius(self, id):
        '''called when a fence is right-clicked and change-circle-radius is selected; next click sets the radius
        '''
        (seq, t) = id.split(":")
        self.setting_circle_radius = int(seq)

    def polyfence_remove_returnpoint(self, id):
        '''called when a returnpoint is right-clicked and remove is selected;
        removes the return point
        '''
        (seq, type) = id.split(":")
        self.module('fence').removereturnpoint(int(seq))

    def polyfence_remove_polygon(self, id):
        '''called when a fence is right-clicked and remove is selected;
        removes the polygon
        '''
        (seq, type) = id.split(":")
        self.module('fence').removepolygon(int(seq))

    def polyfence_remove_polygon_point(self, id, extra):
        '''called when a fence is right-clicked and remove point is selected;
        removes the polygon point
        '''
        (seq, type) = id.split(":")
        self.module('fence').removepolygon_point(int(seq), int(extra))

    def polyfence_add_polygon_point(self, id, extra):
        '''called when a fence is right-clicked and add point is selected;
        adds a polygon *before* this one in the list
        '''
        (seq, type) = id.split(":")
        self.module('fence').addpolygon_point(int(seq), int(extra))

    def polyfence_move_polygon_point(self, id, extra):
        '''called when a fence is right-clicked and move point is selected; start
        moving the polygon point
        '''
        (seq, t) = id.split(":")
        self.moving_polygon_point = (int(seq), extra)
    # End: handling of PolyFence popup menu items

    def display_polyfences_circles(self, circles, colour):
        '''draws circles in the PolyFence layer with colour colour'''
        for circle in circles:
            lat = circle.x
            lng = circle.y
            if circle.get_type() == 'MISSION_ITEM_INT':
                lat *= 1e-7
                lng *= 1e-7
            items = [
                MPMenuItem('Remove Circle', returnkey='popupPolyFenceRemoveCircle'),
                MPMenuItem('Move Circle', returnkey='popupPolyFenceMoveCircle'),
                MPMenuItem('Set Circle Radius w/click', returnkey='popupPolyFenceSetCircleRadius'),
            ]
            popup = MPMenuSubMenu('Popup', items)
            slipcircle = mp_slipmap.SlipCircle(
                str(circle.seq) + ":circle", # key
                "PolyFence", # layer
                (lat, lng), # latlon
                circle.param1, # radius
                colour,
                linewidth=2,
                popup_menu=popup)
            self.map.add_object(slipcircle)

    def display_polyfences_inclusion_circles(self):
        '''draws inclusion circles in the PolyFence layer with colour colour'''
        inclusions = self.module('fence').inclusion_circles()
        self.display_polyfences_circles(inclusions, (0, 255, 0))

    def display_polyfences_exclusion_circles(self):
        '''draws exclusion circles in the PolyFence layer with colour colour'''
        exclusions = self.module('fence').exclusion_circles()
        self.display_polyfences_circles(exclusions, (255, 0, 0))

    def display_polyfences_polygons(self, polygons, colour):
        '''draws polygons in the PolyFence layer with colour colour'''
        for polygon in polygons:
            points = []
            for point in polygon:
                lat = point.x
                lng = point.y
                if point.get_type() == 'MISSION_ITEM_INT':
                    lat *= 1e-7
                    lng *= 1e-7
                points.append((lat, lng))
            items = [
                MPMenuItem('Remove Polygon', returnkey='popupPolyFenceRemovePolygon'),
            ]
            if len(points) > 3:
                items.append(MPMenuItem('Remove Polygon Point', returnkey='popupPolyFenceRemovePolygonPoint'))
            items.append(MPMenuItem('Move Polygon Point', returnkey='popupPolyFenceMovePolygonPoint'))
            items.append(MPMenuItem('Add Polygon Point', returnkey='popupPolyFenceAddPolygonPoint'))

            popup = MPMenuSubMenu('Popup', items)
            poly = mp_slipmap.UnclosedSlipPolygon(
                str(polygon[0].seq) + ":poly", # key,
                points,
                layer='PolyFence',
                linewidth=2,
                colour=colour,
                popup_menu=popup)
            self.map.add_object(poly)

    def display_polyfences_returnpoint(self):
        returnpoint = self.module('fence').returnpoint()

        if returnpoint is None:
            return

        lat = returnpoint.x
        lng = returnpoint.y

        if returnpoint.get_type() == 'MISSION_ITEM_INT':
            lat *= 1e-7
            lng *= 1e-7

        popup = MPMenuSubMenu('Popup', [
            MPMenuItem('Remove Return Point', returnkey='popupPolyFenceRemoveReturnPoint'),
        ])
        self.map.add_object(mp_slipmap.SlipCircle(
            str(returnpoint.seq) + ":returnpoint", # key
            'PolyFence',
            (lat, lng),
            10,
            (255,127,127),
            2,
            popup_menu=popup,
        ))

    def display_polyfences_inclusion_polygons(self):
        '''draws inclusion polygons in the PolyFence layer with colour colour'''
        inclusions = self.module('fence').inclusion_polygons()
        self.display_polyfences_polygons(inclusions, (0, 255, 0))

    def display_polyfences_exclusion_polygons(self):
        '''draws exclusion polygons in the PolyFence layer with colour colour'''
        exclusions = self.module('fence').exclusion_polygons()
        self.display_polyfences_polygons(exclusions, (255, 0, 0))

    def display_polyfences(self):
        '''draws PolyFence items in the PolyFence layer'''
        self.map.add_object(mp_slipmap.SlipClearLayer('PolyFence'))
        self.display_polyfences_inclusion_circles()
        self.display_polyfences_exclusion_circles()
        self.display_polyfences_inclusion_polygons()
        self.display_polyfences_exclusion_polygons()
        self.display_polyfences_returnpoint()

    def display_fence(self):
        '''display the fence'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if getattr(self.module('fence'), "cmd_addcircle", None) is not None:
            # we're doing fences via MissionItemProtocol and thus have
            # much more work to do
            return self.display_polyfences()

        # traditional module, a single polygon fence transfered using
        # FENCE_POINT protocol:
        points = self.module('fence').fenceloader.polygon()
        self.map.add_object(mp_slipmap.SlipClearLayer('Fence'))
        if len(points) > 1:
            popup = MPMenuSubMenu('Popup',
                                  items=[MPMenuItem('FencePoint Remove', returnkey='popupFenceRemove'),
                                         MPMenuItem('FencePoint Move', returnkey='popupFenceMove')])
            self.map.add_object(mp_slipmap.SlipPolygon('Fence', points, layer=1,
                                                               linewidth=2, colour=(0,255,0), popup_menu=popup))
        else:
            self.map.remove_object('Fence')

    def closest_waypoint(self, latlon):
        '''find closest waypoint to a position'''
        (lat, lon) = latlon
        best_distance = -1
        closest = -1
        for i in range(self.module('wp').wploader.count()):
            w = self.module('wp').wploader.wp(i)
            distance = mp_util.gps_distance(lat, lon, w.x, w.y)
            if best_distance == -1 or distance < best_distance:
                best_distance = distance
                closest = i
        if best_distance < 20:
            return closest
        else:
            return -1

    def remove_rally(self, key):
        '''remove a rally point'''
        a = key.split(' ')
        if a[0] != 'Rally' or len(a) != 2:
            print("Bad rally object %s" % key)
            return
        i = int(a[1])
        self.mpstate.functions.process_stdin('rally remove %u' % i)

    def move_rally(self, key):
        '''move a rally point'''
        a = key.split(' ')
        if a[0] != 'Rally' or len(a) != 2:
            print("Bad rally object %s" % key)
            return
        i = int(a[1])
        self.moving_rally = i

    def selection_index_to_idx(self, key, selection_index):
        '''return a mission idx from a selection_index'''
        a = key.split(' ')
        if a[0] != 'mission' or len(a) != 2:
            print("Bad mission object %s" % key)
            return None
        midx = int(a[1])
        if midx < 0 or midx >= len(self.mission_list):
            print("Bad mission index %s" % key)
            return None
        mlist = self.mission_list[midx]
        if selection_index < 0 or selection_index >= len(mlist):
            print("Bad mission polygon %s" % selection_index)
            return None
        idx = mlist[selection_index]
        return idx

    def move_mission(self, key, selection_index):
        '''move a mission point'''
        idx = self.selection_index_to_idx(key, selection_index)
        self.moving_wp = idx
        print("Moving wp %u" % idx)

    def remove_mission(self, key, selection_index):
        '''remove a mission point'''
        idx = self.selection_index_to_idx(key, selection_index)
        self.mpstate.functions.process_stdin('wp remove %u' % idx)

    def split_mission_wp(self, key, selection_index):
        '''add wp before this one'''
        idx = self.selection_index_to_idx(key, selection_index)
        self.mpstate.functions.process_stdin('wp split %u' % idx)

    def remove_fencepoint(self, key, selection_index):
        '''remove a fence point'''
        self.mpstate.functions.process_stdin('fence remove %u' % (selection_index+1))

    def move_fencepoint(self, key, selection_index):
        '''move a fence point'''
        self.moving_fencepoint = selection_index
        print("Moving fence point %u" % selection_index)

    def set_mission(self, key, selection_index):
        '''set a mission point'''
        idx = self.selection_index_to_idx(key, selection_index)
        self.mpstate.functions.process_stdin('wp set %u' % idx)

    def handle_menu_event(self, obj):
        '''handle a popup menu event from the map'''
        menuitem = obj.menuitem
        if menuitem.returnkey.startswith('# '):
            cmd = menuitem.returnkey[2:]
            if menuitem.handler is not None:
                if menuitem.handler_result is None:
                    return
                cmd += menuitem.handler_result
            self.mpstate.functions.process_stdin(cmd)
        elif menuitem.returnkey == 'popupRallyRemove':
            self.remove_rally(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupRallyMove':
            self.move_rally(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupMissionSet':
            self.set_mission(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupMissionRemove':
            self.remove_mission(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupMissionMove':
            self.move_mission(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupMissionSplit':
            self.split_mission_wp(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupFenceMove':
            self.move_fencepoint(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupPolyFenceRemoveCircle':
            self.polyfence_remove_circle(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupPolyFenceMoveCircle':
            self.polyfence_move_circle(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupPolyFenceSetCircleRadius':
            self.polyfence_set_circle_radius(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupPolyFenceRemoveReturnPoint':
            self.polyfence_remove_returnpoint(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupPolyFenceRemovePolygon':
            self.polyfence_remove_polygon(obj.selected[0].objkey)
        elif menuitem.returnkey == 'popupPolyFenceMovePolygonPoint':
            self.polyfence_move_polygon_point(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupPolyFenceAddPolygonPoint':
            self.polyfence_add_polygon_point(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'popupPolyFenceRemovePolygonPoint':
            self.polyfence_remove_polygon_point(obj.selected[0].objkey, obj.selected[0].extra_info)
        elif menuitem.returnkey == 'showPosition':
            self.show_position()
        elif menuitem.returnkey == 'printGoogleMapsLink':
            self.print_google_maps_link()
        elif menuitem.returnkey == 'setServiceTerrain':
            self.module('terrain').cmd_terrain(['set', 'source', menuitem.get_choice()])
        elif menuitem.returnkey == 'showTerrainContours':
            self.display_terrain_contours()
        elif menuitem.returnkey == 'hideTerrainContours':
            self.hide_terrain_contours()
        elif menuitem.returnkey == 'removeTerrainContours':
            self.remove_terrain_contours()

    def map_callback(self, obj):
        '''called when an event happens on the slipmap'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if isinstance(obj, mp_slipmap.SlipMenuEvent):
            self.handle_menu_event(obj)
            return
        if not isinstance(obj, mp_slipmap.SlipMouseEvent):
            return
        if obj.event.leftIsDown and self.moving_rally is not None:
            self.mpstate.click(obj.latlon)
            self.mpstate.functions.process_stdin("rally move %u" % self.moving_rally)
            self.moving_rally = None
            return
        if obj.event.rightIsDown and self.moving_rally is not None:
            print("Cancelled rally move")
            self.moving_rally = None
            return
        if obj.event.leftIsDown and self.moving_wp is not None:
            self.mpstate.click(obj.latlon)
            self.mpstate.functions.process_stdin("wp move %u" % self.moving_wp)
            self.moving_wp = None
            return
        if obj.event.leftIsDown and self.moving_fencepoint is not None:
            self.mpstate.click(obj.latlon)
            self.mpstate.functions.process_stdin("fence move %u" % (self.moving_fencepoint+1))
            self.moving_fencepoint = None
            return
        if obj.event.rightIsDown and self.moving_wp is not None:
            print("Cancelled wp move")
            self.moving_wp = None
            return
        if obj.event.leftIsDown and self.moving_polygon_point is not None:
            self.mpstate.click(obj.latlon)
            (seq, offset) = self.moving_polygon_point
            self.mpstate.functions.process_stdin("fence movepolypoint %u %u" % (int(seq), int(offset)))
            self.moving_polygon_point = None
            return
        if obj.event.rightIsDown and self.moving_polygon_point is not None:
            print("Cancelled polygon point move")
            self.moving_polygon_point = None
            return
        if obj.event.rightIsDown and self.moving_fencepoint is not None:
            print("Cancelled fence move")
            self.moving_fencepoint = None
            return
        elif obj.event.leftIsDown:
            if (self.mpstate.click_time is None or
                time.time() - self.mpstate.click_time > 0.1):
                self.mpstate.click(obj.latlon)
                self.drawing_update()

        if obj.event.rightIsDown:
            if self.draw_callback is not None:
                self.drawing_end()
                return
            if (self.mpstate.click_time is None or
                time.time() - self.mpstate.click_time > 0.1):
                self.mpstate.click(obj.latlon)

        if obj.event.leftIsDown and self.moving_circle is not None:
            self.mpstate.click(obj.latlon)
            seq = self.moving_circle
            self.mpstate.functions.process_stdin("fence movecircle %u" % int(seq))
            self.moving_circle = None
            return
        if obj.event.rightIsDown and self.moving_circle is not None:
            print("Cancelled circle move")
            self.moving_circle = None
            return

        if obj.event.leftIsDown and self.setting_circle_radius is not None:
            self.mpstate.click(obj.latlon)
            seq = self.setting_circle_radius
            self.mpstate.functions.process_stdin("fence setcircleradius %u" % int(seq))
            self.setting_circle_radius = None
            return
        if obj.event.rightIsDown and self.setting_circle_radius is not None:
            print("Cancelled circle move")
            self.setting_circle_radius = None
            return

    def click_updated(self):
        '''called when the click position has changed'''
        if self.map_settings.showclicktime == 0:
            return
        self.map.add_object(mp_slipmap.SlipClickLocation(self.mpstate.click_location, timeout=self.map_settings.showclicktime))

    def unload(self):
        '''unload module'''
        super(MapModule, self).unload()
        self.map.close()
        if self.instance == 1:
            self.mpstate.map = None
            self.mpstate.map_functions = {}

    def idle_task(self):
        now = time.time()
        if self.last_unload_check_time + self.unload_check_interval < now:
            self.last_unload_check_time = now
            if not self.map.is_alive():
                self.needs_unloading = True

        # check for any events from the map
        self.map.check_events()

    def create_vehicle_icon(self, name, colour, follow=False, vehicle_type=None):
        '''add a vehicle to the map'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if vehicle_type is None:
            vehicle_type = self.vehicle_type_name
        if name in self.have_vehicle and self.have_vehicle[name] == vehicle_type:
            return
        self.have_vehicle[name] = vehicle_type
        icon = self.map.icon(colour + vehicle_type + '.png')
        self.map.add_object(mp_slipmap.SlipIcon(name, (0,0), icon, layer=3, rotation=0, follow=follow,
                                                   trail=mp_slipmap.SlipTrail()))

    def remove_vehicle_icon(self, name, vehicle_type=None):
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if vehicle_type is None:
            vehicle_type = self.vehicle_type_name
        if name not in self.have_vehicle or self.have_vehicle[name] != vehicle_type:
            return
        del self.have_vehicle[name]
        self.map.remove_object(name)

    def drawing_update(self):
        '''update line drawing'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if self.draw_callback is None:
            return
        self.draw_line.append(self.mpstate.click_location)
        if len(self.draw_line) > 1:
            self.map.add_object(mp_slipmap.SlipPolygon('drawing', self.draw_line,
                                                          layer='Drawing', linewidth=2, colour=self.draw_colour))

    def drawing_end(self):
        '''end line drawing'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        if self.draw_callback is None:
            return
        self.draw_callback(self.draw_line)
        self.draw_callback = None
        self.map.add_object(mp_slipmap.SlipDefaultPopup(self.default_popup, combine=True))
        self.map.add_object(mp_slipmap.SlipClearLayer('Drawing'))

    def draw_lines(self, callback, colour=(128,128,255)):
        '''draw a series of connected lines on the map, calling callback when done'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        self.draw_callback = callback
        self.draw_colour = colour
        self.draw_line = []
        self.map.add_object(mp_slipmap.SlipDefaultPopup(None))

    def cmd_set_home(self, args):
        '''called when user selects "Set Home (with height)" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        alt = self.module('terrain').ElevationModel.GetElevation(lat, lon)
        print("Setting home to: ", lat, lon, alt)
        self.master.mav.command_long_send(
            self.settings.target_system, self.settings.target_component,
            mavutil.mavlink.MAV_CMD_DO_SET_HOME,
            1, # set position
            0, # param1
            0, # param2
            0, # param3
            0, # param4
            lat, # lat
            lon, # lon
            alt) # param7

    def cmd_set_homepos(self, args):
        '''called when user selects "Set Home" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        print("Setting home to: ", lat, lon)
        self.master.mav.command_int_send(
            self.settings.target_system, self.settings.target_component,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            mavutil.mavlink.MAV_CMD_DO_SET_HOME,
            1, # current
            0, # autocontinue
            0, # param1
            0, # param2
            0, # param3
            0, # param4
            int(lat*1e7), # lat
            int(lon*1e7), # lon
            0) # no height change

    def cmd_set_roi(self, args):
        '''called when user selects "Set ROI" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        alt = self.module('terrain').ElevationModel.GetElevation(lat, lon)
        print("Setting ROI to: ", lat, lon, alt)
        self.current_ROI = (lat,lon,alt)
        self.master.mav.command_int_send(
            self.settings.target_system, self.settings.target_component,
            mavutil.mavlink.MAV_FRAME_GLOBAL,
            mavutil.mavlink.MAV_CMD_DO_SET_ROI_LOCATION,
            0, # current
            0, # autocontinue
            0, # param1
            0, # param2
            0, # param3
            0, # param4
            int(lat*1e7), # lat
            int(lon*1e7), # lon
            alt) # param7

    def cmd_set_position(self, args):
        '''called when user selects "Set Position" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        accuracy = self.map_settings.setpos_accuracy
        print("Setting position to (%.7f %.7f) with accuracy %.1fm" % (lat, lon, accuracy))
        now = time.time()
        self.master.mav.command_int_send(
            self.settings.target_system, self.settings.target_component,
            mavutil.mavlink.MAV_FRAME_GLOBAL,
            mavutil.mavlink.MAV_CMD_EXTERNAL_POSITION_ESTIMATE,
            0, # current
            0, # autocontinue
            time.time() - self.mpstate.start_time_s, # transmission_time
            0, # processing_time
            self.map_settings.setpos_accuracy, # accuracy
            0, # param4
            int(lat*1e7), # lat
            int(lon*1e7), # lon
            float('NaN')) # alt, send as NaN for ignore
            
    def cmd_set_origin(self, args):
        '''called when user selects "Set Origin (with height)" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        alt = self.module('terrain').ElevationModel.GetElevation(lat, lon)
        print("Setting origin to: ", lat, lon, alt)
        self.master.mav.set_gps_global_origin_send(
            self.settings.target_system,
            int(lat*10000000), # lat
            int(lon*10000000), # lon
            int(alt*1000)) # param7

    def cmd_set_originpos(self, args):
        '''called when user selects "Set Origin" on map'''
        (lat, lon) = (self.mpstate.click_location[0], self.mpstate.click_location[1])
        print("Setting origin to: ", lat, lon)
        self.master.mav.set_gps_global_origin_send(
            self.settings.target_system,
            int(lat*10000000), # lat
            int(lon*10000000), # lon
            0*1000) # no height change

    def cmd_zoom(self, args):
        '''control zoom'''
        if len(args) < 2:
            print("map zoom WIDTH(m)")
            return
        ground_width = float(args[1])
        self.map.set_zoom(ground_width)

    def cmd_center(self, args):
        '''control center of view'''
        if len(args) < 3:
            print("map center LAT LON")
            return
        lat = float(args[1])
        lon = float(args[2])
        self.map.set_center(lat, lon)

    def cmd_follow(self, args):
        '''control following of vehicle'''
        if len(args) < 2:
            print("map follow 0|1")
            return
        follow = int(args[1])
        self.map.set_follow(follow)

    def cmd_clear(self, args):
        '''clear displayed vehicle icons'''
        self.map.add_object(mp_slipmap.SlipClearLayer(3))
        self.have_vehicle = {}

    def set_secondary_vehicle_position(self, m):
        '''show 2nd vehicle on map'''
        if m.get_type() != 'GLOBAL_POSITION_INT':
            return
        (lat, lon, heading) = (m.lat*1.0e-7, m.lon*1.0e-7, m.hdg*0.01)
        if abs(lat) < 1.0e-3 and abs(lon) > 1.0e-3:
            return
        # hack for OBC2016
        alt = self.module('terrain').ElevationModel.GetElevation(lat, lon)
        agl = m.alt * 0.001 - alt
        agl_s = str(int(agl)) + 'm'
        self.create_vehicle_icon('VehiclePos2', 'blue', follow=False, vehicle_type='plane')
        self.map.set_position('VehiclePos2', (lat, lon), rotation=heading, label=agl_s, colour=(0,255,255))

    def update_vehicle_icon(self, name, vehicle, colour, m, display):
        '''update display of a vehicle on the map.  m is expected to store
        location in lat/lng *1e7
        '''
        latlon = (m.lat*1.0e-7, m.lng*1.0e-7)
        yaw = math.degrees(m.yaw)
        self.update_vehicle_icon_to_loc(name, vehicle, colour, display, latlon, yaw)

    def update_vehicle_icon_to_loc(self, name, vehicle, colour, display, latlon, yaw):
        '''update display of a vehicle on the map at latlon
        '''
        key = name + vehicle

        # don't display this icon if the settings don't say so:
        if not display:
            # remove from display if it was being displayed:
            self.remove_vehicle_icon(key)
            return

        # create the icon if we weren't displaying:
        self.create_vehicle_icon(key, colour)

        # update placement on map:
        self.map.set_position(key, latlon, rotation=yaw)

    def mavlink_packet(self, m):
        '''handle an incoming mavlink packet'''
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        mtype = m.get_type()
        sysid = m.get_srcSystem()

        if mtype == "HEARTBEAT" or mtype == "HIGH_LATENCY2":
            vname = None
            vtype = self.vehicle_type_override.get(sysid, m.type)
            if vtype in [mavutil.mavlink.MAV_TYPE_FIXED_WING,
                            mavutil.mavlink.MAV_TYPE_VTOL_DUOROTOR,
                            mavutil.mavlink.MAV_TYPE_VTOL_QUADROTOR,
                            mavutil.mavlink.MAV_TYPE_VTOL_TILTROTOR]:
                vname = 'plane'
            elif vtype in [mavutil.mavlink.MAV_TYPE_GROUND_ROVER]:
                vname = 'rover'
            elif vtype in [mavutil.mavlink.MAV_TYPE_SUBMARINE]:
                vname = 'sub'
            elif vtype in [mavutil.mavlink.MAV_TYPE_SURFACE_BOAT]:
                vname = 'boat'
            elif vtype in [mavutil.mavlink.MAV_TYPE_QUADROTOR,
                            mavutil.mavlink.MAV_TYPE_HEXAROTOR,
                            mavutil.mavlink.MAV_TYPE_OCTOROTOR,
                            mavutil.mavlink.MAV_TYPE_TRICOPTER,
                            mavutil.mavlink.MAV_TYPE_DODECAROTOR,
                            mavutil.mavlink.MAV_TYPE_DECAROTOR]:
                vname = 'copter'
            elif vtype in [mavutil.mavlink.MAV_TYPE_COAXIAL]:
                vname = 'singlecopter'
            elif vtype in [mavutil.mavlink.MAV_TYPE_HELICOPTER]:
                vname = 'heli'
            elif vtype in [mavutil.mavlink.MAV_TYPE_ANTENNA_TRACKER]:
                vname = 'antenna'
            elif vtype in [mavutil.mavlink.MAV_TYPE_AIRSHIP]:
                vname = 'blimp'
            if vname is not None:
                self.vehicle_type_by_sysid[sysid] = vname

        if not sysid in self.vehicle_type_by_sysid:
            self.vehicle_type_by_sysid[sysid] = 'plane'
        self.vehicle_type_name = self.vehicle_type_by_sysid[sysid]

        # this is the beginnings of allowing support for multiple vehicles
        # in the air at the same time
        vehicle = 'Vehicle%u' % m.get_srcSystem()

        if mtype == "SIMSTATE":
            self.update_vehicle_icon('Sim', vehicle, 'green', m, self.map_settings.showsimpos)
        elif mtype == "AHRS2" and self.map_settings.showahrs2pos:
            self.update_vehicle_icon('AHRS2', vehicle, 'purple', m, self.map_settings.showahrs2pos)
        elif mtype == "AHRS3" and self.map_settings.showahrs3pos:
            self.update_vehicle_icon('AHRS3', vehicle, 'orange', m, self.map_settings.showahrs3pos)
        elif mtype == "GPS_RAW_INT":
            (lat, lon) = (m.lat*1.0e-7, m.lon*1.0e-7)
            if lat != 0 or lon != 0:
                if m.vel > 300 or m.get_srcSystem() not in self.lat_lon_heading:
                    heading = m.cog*0.01
                else:
                    (_,_,heading) = self.lat_lon_heading[m.get_srcSystem()]
                self.update_vehicle_icon_to_loc('GPS', vehicle, 'blue', self.map_settings.showgpspos, (lat, lon), heading)
        elif mtype == "GPS2_RAW":
            (lat, lon) = (m.lat*1.0e-7, m.lon*1.0e-7)
            if lat != 0 or lon != 0:
                self.update_vehicle_icon_to_loc('GPS2', vehicle, 'green', self.map_settings.showgps2pos, (lat, lon), m.cog*0.01)
        elif mtype == 'GLOBAL_POSITION_INT':
            (lat, lon, heading) = (m.lat*1.0e-7, m.lon*1.0e-7, m.hdg*0.01)
            self.lat_lon_heading[m.get_srcSystem()] = (lat,lon,heading)
            if self.map_settings.showahrspos:
                if abs(lat) > 1.0e-3 or abs(lon) > 1.0e-3:
                    self.have_global_position = True
                    self.create_vehicle_icon('Pos' + vehicle, 'red', follow=True)
                    if len(self.vehicle_type_by_sysid) > 1:
                        label = str(sysid)
                    else:
                        label = None
                    self.map.set_position('Pos' + vehicle, (lat, lon), rotation=heading, label=label, colour=(255,255,255))
                    self.map.set_follow_object('Pos' + vehicle, self.message_is_from_primary_vehicle(m))
            else:
                self.remove_vehicle_icon('Pos' + vehicle)

        elif mtype == "HIGH_LATENCY2" and self.map_settings.showahrspos:
            (lat, lon) = (m.latitude*1.0e-7, m.longitude*1.0e-7)
            if lat != 0 or lon != 0:
                cog = m.heading * 2
                self.have_global_position = True
                self.create_vehicle_icon('Pos' + vehicle, 'red', follow=True)
                if len(self.vehicle_type_by_sysid) > 1:
                    label = str(sysid)
                else:
                    label = None
                self.map.set_position('Pos' + vehicle, (lat, lon), rotation=cog, label=label, colour=(255,255,255))
                self.map.set_follow_object('Pos' + vehicle, self.message_is_from_primary_vehicle(m))

        elif mtype == 'HOME_POSITION':
            (lat, lon) = (m.latitude*1.0e-7, m.longitude*1.0e-7)
            icon = self.map.icon('home.png')
            self.map.add_object(mp_slipmap.SlipIcon('HOME_POSITION',
                                                            (lat,lon),
                                                            icon, layer=3, rotation=0, follow=False))

        elif mtype == "NAV_CONTROLLER_OUTPUT":
            tlayer = 'Trajectory%u' % m.get_srcSystem()
            if (self.master.flightmode in [ "AUTO", "GUIDED", "LOITER", "RTL", "QRTL", "QLOITER", "QLAND", "FOLLOW", "ZIGZAG" ] and
                m.get_srcSystem() in self.lat_lon_heading):
                (lat,lon,_) = self.lat_lon_heading[m.get_srcSystem()]
                trajectory = [ (lat, lon),
                                mp_util.gps_newpos(lat, lon, m.target_bearing, m.wp_dist) ]
                self.map.add_object(mp_slipmap.SlipPolygon('trajectory',
                                                           trajectory, layer=tlayer,
                                                               linewidth=2, colour=(255,0,180)))
                self.trajectory_layers.add(tlayer)
            else:
                if tlayer in self.trajectory_layers:
                    self.map.add_object(mp_slipmap.SlipClearLayer(tlayer))
                    self.trajectory_layers.remove(tlayer)

        elif mtype == "POSITION_TARGET_GLOBAL_INT":
            # FIXME: base this off SYS_STATUS.MAV_SYS_STATUS_SENSOR_XY_POSITION_CONTROL?
            if not m.get_srcSystem() in self.lat_lon_heading:
                return
            tlayer = 'PostionTarget%u' % m.get_srcSystem()
            (lat,lon,_) = self.lat_lon_heading[m.get_srcSystem()]
            if (self.master.flightmode in [ "AUTO", "GUIDED", "LOITER", "RTL", "QRTL", "QLOITER", "QLAND", "FOLLOW" ]):
                lat_float = m.lat_int*1e-7
                lon_float = m.lon_int*1e-7
                vec = [ (lat_float, lon_float),
                        (lat, lon) ]
                self.map.add_object(mp_slipmap.SlipPolygon('position_target',
                                                           vec,
                                                           layer=tlayer,
                                                           linewidth=2,
                                                           colour=(0,255,0)))
            else:
                self.map.add_object(mp_slipmap.SlipClearLayer(tlayer))

        if not self.message_is_from_primary_vehicle(m):
            # the rest should only be done for the primary vehicle
            return

        self.check_redisplay_waypoints()
        self.check_redisplay_fencepoints()
        self.check_redisplay_rallypoints()

        # check for any events from the map
        self.map.check_events()

    def check_redisplay_waypoints(self):
        # if the waypoints have changed, redisplay
        wp_module = self.module('wp')
        if wp_module is None:
            '''wp nodule not loaded'''
            return
        last_wp_change = wp_module.wploader.last_change
        if self.wp_change_time != last_wp_change and abs(time.time() - last_wp_change) > 1:
            self.wp_change_time = last_wp_change
            self.display_waypoints()

            #this may have affected the landing lines from the rally points:
            self.rally_change_time = time.time()

    def check_redisplay_fencepoints(self):
        # if the fence has changed, redisplay
        fence_module = self.module('fence')
        if fence_module is not None:
            if hasattr(fence_module, 'last_change'):
                # new fence module
                last_change = fence_module.last_change()
            else:
                # old fence module
                last_change = fence_module.fenceloader.last_change
            if self.fence_change_time != last_change:
                self.fence_change_time = last_change
                self.display_fence()

    def check_redisplay_rallypoints(self):
        # if the rallypoints have changed, redisplay
        if (self.module('rally') and
            self.rally_change_time != self.module('rally').last_change()):
            self.rally_change_time = self.module('rally').last_change()
            icon = self.map.icon('rallypoint.png')
            self.map.add_object(mp_slipmap.SlipClearLayer('RallyPoints'))
            for i in range(self.module('rally').rally_count()):
                rp = self.module('rally').rally_point(i)
                popup = MPMenuSubMenu('Popup',
                                      items=[MPMenuItem('Rally Remove', returnkey='popupRallyRemove'),
                                             MPMenuItem('Rally Move', returnkey='popupRallyMove')])
                self.map.add_object(mp_slipmap.SlipIcon('Rally %u' % (i+1), (rp.lat*1.0e-7, rp.lng*1.0e-7), icon,
                                                                layer='RallyPoints', rotation=0, follow=False,
                                                                popup_menu=popup))

                loiter_rad = self.get_mav_param('WP_LOITER_RAD')

                if self.map_settings.rallycircle:
                    self.map.add_object(mp_slipmap.SlipCircle('Rally Circ %u' % (i+1), 'RallyPoints', (rp.lat*1.0e-7, rp.lng*1.0e-7),
                                                                      loiter_rad, (255,255,0), 2, arrow = self.map_settings.showdirection))

                #draw a line between rally point and nearest landing point
                nearest_land_wp = None
                nearest_distance = 10000000.0
                for j in range(self.module('wp').wploader.count()):
                    w = self.module('wp').wploader.wp(j)
                    if (w.command == 21): #if landing waypoint
                        #get distance between rally point and this waypoint
                        dis = mp_util.gps_distance(w.x, w.y, rp.lat*1.0e-7, rp.lng*1.0e-7)
                        if (dis < nearest_distance):
                            nearest_land_wp = w
                            nearest_distance = dis

                if nearest_land_wp is not None:
                    points = []
                    #tangential approach?
                    if self.get_mav_param('LAND_BREAK_PATH') == 0:
                        theta = math.degrees(math.atan(loiter_rad / nearest_distance))
                        tan_dis = math.sqrt(nearest_distance * nearest_distance - (loiter_rad * loiter_rad))

                        ral_bearing = mp_util.gps_bearing(nearest_land_wp.x, nearest_land_wp.y,rp.lat*1.0e-7, rp.lng*1.0e-7)

                        points.append(mp_util.gps_newpos(nearest_land_wp.x,nearest_land_wp.y, ral_bearing + theta, tan_dis))

                    else: #not tangential approach
                        points.append((rp.lat*1.0e-7, rp.lng*1.0e-7))

                    points.append((nearest_land_wp.x, nearest_land_wp.y))
                    self.map.add_object(mp_slipmap.SlipPolygon('Rally Land %u' % (i+1), points, 'RallyPoints', (255,255,0), 2))

    def display_terrain_contours(self):
        """
        Show terrain contours
        """
        from MAVProxy.modules.mavproxy_map import mp_slipmap
        import numpy as np

        # configure matplotlib for non-gui use
        import matplotlib
        matplotlib.use('Agg') 

        # disable interactive plotting mode
        import matplotlib.pyplot as plt
        plt.ioff()

        terrain_module = self.module('terrain')
        if terrain_module is None:
            return

        elevation_model = terrain_module.ElevationModel

        # show contours if they have already been calculated
        if len(self.terrain_contour_ids) > 0:
            self.show_terrain_contours()
            return

        # centre terrain grid about clicked location
        if self.mpstate.click_location is None:
            return

        (lat, lon) = self.mpstate.click_location

        # retrieve grid options from map settings
        grid_spacing = self.map_settings.contour_grid_spacing
        grid_extent = self.map_settings.contour_grid_extent
        levels = self.map_settings.contour_levels

        # create mesh grid
        x = np.arange(-0.5 * grid_extent, 0.5 * grid_extent, grid_spacing)
        y = np.arange(-0.5 * grid_extent, 0.5 * grid_extent, grid_spacing)
        x_grid, y_grid = np.meshgrid(x, y)

        def terrain_surface(lat, lon, x, y):
            """
            Calculate terrain altitudes for the NED offsets (x, y)
            centred on (lat, lon).
            """
            alt = []
            for east in y:
                alt_y = []
                for north in x:
                    (lat2, lon2) = mp_util.gps_offset(lat, lon, east, north)
                    alt_y.append(elevation_model.GetElevation(lat2, lon2))
                alt.append(alt_y)
            return alt

        def ned_to_latlon(contours, lat, lon):
            """
            Convert contour polygons in NED coordinates offset from (lat, lon)
            to polygons in orthometric coordinates.
            """
            contours_latlon = []
            for polygons in contours:
                polygons_latlon = []
                for polygon in polygons:
                    polygon_latlon = []
                    for point in polygon:
                        (north, east) = point
                        (lat2, lon2) = mp_util.gps_offset(lat, lon, east, north)
                        polygon_latlon.append([lat2, lon2])
                    polygons_latlon.append(polygon_latlon)
                contours_latlon.append(polygons_latlon)
            return contours_latlon

        # generate surface and contours
        z_grid = np.array(terrain_surface(lat, lon, x, y))
        _, (ax1) = plt.subplots(1, 1, figsize=(10,10))
        cs = ax1.contour(x_grid, y_grid, z_grid, levels=levels)
        contours = ned_to_latlon(cs.allsegs, lat, lon)

        # add terrain layer and contour polygons
        self.map.add_object(mp_slipmap.SlipClearLayer('Terrain'))

        self.terrain_contour_ids.clear()
        num_contours = len(contours)
        for i in range(num_contours):
            polygons = contours[i]
            for j in range(len(polygons)):
                p = polygons[j]
                if len(p) > 1:
                    id = f"terrain {i} {j}" 
                    self.terrain_contour_ids.append(id)
                    contour_colour = (255, 255, 255)
                    self.map.add_object(mp_slipmap.SlipPolygon(
                        id, p,
                        layer='Terrain', linewidth=1,
                        colour=contour_colour,
                        showcircles=False
                    ))

    def show_terrain_contours(self):
        """
        Show terrain contours.
        """
        # unhide polygons
        for id in self.terrain_contour_ids:
            self.map.hide_object(id, hide=False)

    def hide_terrain_contours(self):
        """
        Hide terrain contours.
        """
        # hide polygons
        for id in self.terrain_contour_ids:
            self.map.hide_object(id, hide=True)

    def remove_terrain_contours(self):
        """
        Remove terrain contours and the terrain clear layer.
        """
        # remove polygons
        for id in self.terrain_contour_ids:
            self.map.remove_object(id)
        # remove layer
        self.map.remove_object('Terrain')
        self.terrain_contour_ids.clear()


def init(mpstate):
    '''initialise module'''
    return MapModule(mpstate)
