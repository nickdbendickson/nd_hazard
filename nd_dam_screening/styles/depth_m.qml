<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="0.8" band="1" alphaBand="-1" classificationMin="0.003" classificationMax="3.0" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="0.003" maximumValue="3.0">
          <item value="0.003" color="#deebf7" label="0.003 m" alpha="255"/>
          <item value="0.3" color="#c6dbef" label="0.3 m (1 ft)" alpha="255"/>
          <item value="0.9" color="#6baed6" label="0.9 m (3 ft)" alpha="255"/>
          <item value="1.8" color="#2171b5" label="1.8 m (6 ft)" alpha="255"/>
          <item value="3.0" color="#08306b" label=">= 3 m (9.8 ft)" alpha="255"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" saturation="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
