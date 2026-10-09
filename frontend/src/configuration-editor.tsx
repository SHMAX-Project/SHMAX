import { ConfigurationWorkflow } from './workflow';
import { useNavigate } from 'react-router-dom';
import { StepMax, StepTelegramBot, StepTelegramNetwork, StepTelegramGroup, StepMaxChats, type WizardStepId } from './wizard';

/** Reuse the actual component controls without onboarding progress/next steps. */
export function ConfigurationEditor({ component, reload }: { component: WizardStepId; reload: () => void }) {
  const navigate = useNavigate();
  const target = component === 'max' ? '/max' : component === 'max-chats' ? '/chats' : '/telegram';
  const done = () => { reload(); navigate(target); };
  const props = { onNext: done, onPrev: () => navigate(target) };
  return <ConfigurationWorkflow.Provider value="EDIT_EXISTING_CONFIGURATION">
    {component === 'max' ? <StepMax {...props} /> : component === 'telegram-bot' ? <StepTelegramBot {...props} /> : component === 'telegram-network' ? <StepTelegramNetwork {...props} /> : component === 'max-chats' ? <StepMaxChats {...props} /> : <StepTelegramGroup {...props} />}
  </ConfigurationWorkflow.Provider>;
}
