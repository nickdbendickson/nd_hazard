<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0-Firenze" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="0.8" band="1" alphaBand="-1" classificationMin="0" classificationMax="24" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="0" maximumValue="24">
          <item value="0" color="#00441b" label="0 h" alpha="255"/>
          <item value="1" color="#238b45" label="1 h" alpha="255"/>
          <item value="3" color="#74c476" label="3 h" alpha="255"/>
          <item value="6" color="#c7e9c0" label="6 h" alpha="255"/>
          <item value="24" color="#f7fcf5" label=">= 24 h" alpha="255"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" saturation="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
