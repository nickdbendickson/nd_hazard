<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <renderer-v2 type="categorizedSymbol" attr="sim_status" forceraster="0" symbollevels="0" enableorderby="0">
    <categories>
      <category value="ok" symbol="0" label="ok" render="true"/>
      <category value="FAILED" symbol="1" label="FAILED" render="true"/>
      <category value="triage" symbol="2" label="skipped (triage)" render="true"/>
      <category value="" symbol="3" label="other" render="true"/>
    </categories>
    <symbols>
    <symbol type="marker" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
      <layer class="SimpleMarker" enabled="1" locked="0" pass="0">
        <prop k="color" v="76,175,80,255"/>
        <prop k="name" v="circle"/>
        <prop k="size" v="3"/>
        <prop k="size_unit" v="MM"/>
        <prop k="outline_color" v="35,35,35,255"/>
        <prop k="outline_width" v="0.2"/>
        <prop k="outline_width_unit" v="MM"/>
      </layer>
    </symbol>
    <symbol type="marker" name="1" alpha="1" clip_to_extent="1" force_rhr="0">
      <layer class="SimpleMarker" enabled="1" locked="0" pass="0">
        <prop k="color" v="244,67,54,255"/>
        <prop k="name" v="circle"/>
        <prop k="size" v="3"/>
        <prop k="size_unit" v="MM"/>
        <prop k="outline_color" v="35,35,35,255"/>
        <prop k="outline_width" v="0.2"/>
        <prop k="outline_width_unit" v="MM"/>
      </layer>
    </symbol>
    <symbol type="marker" name="2" alpha="1" clip_to_extent="1" force_rhr="0">
      <layer class="SimpleMarker" enabled="1" locked="0" pass="0">
        <prop k="color" v="158,158,158,255"/>
        <prop k="name" v="circle"/>
        <prop k="size" v="3"/>
        <prop k="size_unit" v="MM"/>
        <prop k="outline_color" v="35,35,35,255"/>
        <prop k="outline_width" v="0.2"/>
        <prop k="outline_width_unit" v="MM"/>
      </layer>
    </symbol>
    <symbol type="marker" name="3" alpha="1" clip_to_extent="1" force_rhr="0">
      <layer class="SimpleMarker" enabled="1" locked="0" pass="0">
        <prop k="color" v="96,125,139,255"/>
        <prop k="name" v="circle"/>
        <prop k="size" v="3"/>
        <prop k="size_unit" v="MM"/>
        <prop k="outline_color" v="35,35,35,255"/>
        <prop k="outline_width" v="0.2"/>
        <prop k="outline_width_unit" v="MM"/>
      </layer>
    </symbol>
    </symbols>
  </renderer-v2>
  <layerGeometryType>0</layerGeometryType>
</qgis>
