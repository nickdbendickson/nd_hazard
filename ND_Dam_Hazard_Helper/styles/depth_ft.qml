<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="0.8" band="1" alphaBand="-1" classificationMin="0.01" classificationMax="10" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="0.01" maximumValue="10">
          <item value="0.01" color="#deebf7" label="0.01 ft" alpha="255"/>
          <item value="1" color="#c6dbef" label="1 ft" alpha="255"/>
          <item value="3" color="#6baed6" label="3 ft" alpha="255"/>
          <item value="6" color="#2171b5" label="6 ft" alpha="255"/>
          <item value="10" color="#08306b" label=">= 10 ft" alpha="255"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" saturation="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
