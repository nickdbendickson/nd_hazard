<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="0.8" band="1" alphaBand="-1" classificationMin="0.03" classificationMax="4.6" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="0.03" maximumValue="4.6">
          <item value="0.03" color="#fee6ce" label="0.03 m/s" alpha="255"/>
          <item value="0.9" color="#fdae6b" label="0.9 m/s (3 ft/s)" alpha="255"/>
          <item value="1.8" color="#f16913" label="1.8 m/s (6 ft/s)" alpha="255"/>
          <item value="3.0" color="#d94801" label="3 m/s (10 ft/s)" alpha="255"/>
          <item value="4.6" color="#7f2704" label=">= 4.6 m/s (15 ft/s)" alpha="255"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" saturation="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
