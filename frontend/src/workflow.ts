import { createContext, useContext } from 'react';

export const ConfigurationWorkflow = createContext<'INITIAL_ONBOARDING' | 'EDIT_EXISTING_CONFIGURATION'>('INITIAL_ONBOARDING');
export const useConfigurationEditing = () => useContext(ConfigurationWorkflow) === 'EDIT_EXISTING_CONFIGURATION';
