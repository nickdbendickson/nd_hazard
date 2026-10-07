<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="0.8" band="1" alphaBand="-1" classificationMin="0.1" classificationMax="15" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="0.1" maximumValue="15">
          <item value="0.1" color="#fee6ce" label="0.1 ft/s" alpha="255"/>
          <item value="3" color="#fdae6b" label="3 ft/s" alpha="255"/>
          <item value="6" color="#f16913" label="6 ft/s" alpha="255"/>
          <item value="10" color="#d94801" label="10 ft/s" alpha="255"/>
          <item value="15" color="#7f2704" label=">= 15 ft/s" alpha="255"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" saturation="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
