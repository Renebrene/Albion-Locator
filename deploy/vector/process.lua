-- Минимальный профиль подложки: дороги, вода, landuse, места, здания.
-- API tilemaker 2.4: методы вызываются на объекте node/way.

node_keys = { "place" }

local ROAD_CLASSES = {
  motorway = 5, trunk = 5, motorway_link = 6, trunk_link = 6,
  primary = 7, primary_link = 8,
  secondary = 8, secondary_link = 9,
  tertiary = 9, tertiary_link = 10,
  residential = 11, unclassified = 11, living_street = 11,
  service = 12, track = 12, path = 13, footway = 13, cycleway = 13, pedestrian = 12
}

function node_function(node)
  local place = node:Find("place")
  if place == "city" or place == "town" or place == "village" or place == "hamlet" then
    local name = node:Find("name")
    if name ~= "" then
      node:Layer("place", false)
      node:Attribute("class", place)
      node:Attribute("name", name)
      if place == "city" then
        node:AttributeNumeric("rank", 4); node:MinZoom(6)
      elseif place == "town" then
        node:AttributeNumeric("rank", 6); node:MinZoom(8)
      elseif place == "village" then
        node:AttributeNumeric("rank", 8); node:MinZoom(10)
      else
        node:AttributeNumeric("rank", 9); node:MinZoom(12)
      end
    end
  end
end

function way_function(way)
  local highway = way:Find("highway")
  local road_zoom = ROAD_CLASSES[highway]
  if road_zoom ~= nil then
    way:Layer("transportation", false)
    way:MinZoom(road_zoom)
    way:Attribute("class", highway)
    local name = way:Find("name")
    if name ~= "" then
      way:Layer("transportation_name", false)
      way:MinZoom(math.max(road_zoom, 10))
      way:Attribute("class", highway)
      way:Attribute("name", name)
    end
    local ref = way:Find("ref")
    if ref ~= "" then way:Attribute("ref", ref) end
  end

  local waterway = way:Find("waterway")
  if waterway == "river" or waterway == "canal" or waterway == "stream" then
    way:Layer("waterway", false)
    way:MinZoom(waterway == "river" and 5 or 9)
    way:Attribute("class", waterway)
  end

  if way:Find("natural") == "water" or way:Find("waterway") == "riverbank"
      or way:Find("landuse") == "reservoir" then
    way:Layer("water", true)
    way:MinZoom(5)
  end

  local landuse = way:Find("landuse")
  local natural = way:Find("natural")
  local leisure = way:Find("leisure")
  local lc = ""
  if landuse == "forest" or natural == "wood" then lc = "forest"
  elseif landuse == "residential" then lc = "residential"
  elseif landuse == "industrial" then lc = "industrial"
  elseif landuse == "commercial" then lc = "commercial"
  elseif landuse == "cemetery" then lc = "cemetery"
  elseif landuse == "farmland" or landuse == "meadow" or landuse == "grass"
      or natural == "grassland" or natural == "scrub" then lc = "grass"
  elseif leisure == "park" or leisure == "nature_reserve" then lc = "park"
  end
  if lc ~= "" then
    way:Layer("landuse", true)
    way:MinZoom(lc == "forest" and 8 or 10)
    way:Attribute("class", lc)
  end

  if way:Find("building") ~= "" then
    way:Layer("building", true)
    way:MinZoom(14)
  end
end
