-- GBRB_20190227 cleanup (run in order)
DELETE FROM alarm_journal
  WHERE alarm_name IN (
    SELECT path FROM tag_definitions WHERE metadata->>'ifix_source' = 'GBRB_20190227'
  );
DELETE FROM alarm_configs
  WHERE tag_path IN (
    SELECT path FROM tag_definitions WHERE metadata->>'ifix_source' = 'GBRB_20190227'
  );
DELETE FROM tag_history
  WHERE tag_path IN (
    SELECT path FROM tag_definitions WHERE metadata->>'ifix_source' = 'GBRB_20190227'
  );
DELETE FROM tag_definitions WHERE metadata->>'ifix_source' = 'GBRB_20190227';
DELETE FROM udt_instances WHERE type_id IN (
  'RoasterStepData','RoasterRecipeAG6','RoasterRecipePoint',
  'ScolariRoaster','BeanRecipeSlot','Silo','PidLoop','MotorOrDrive'
);
DELETE FROM udt_types WHERE name IN (
  'RoasterStepData','RoasterRecipeAG6','RoasterRecipePoint',
  'ScolariRoaster','BeanRecipeSlot','Silo','PidLoop','MotorOrDrive'
);
DELETE FROM devices WHERE config->>'ifix_source' = 'GBRB_20190227';
