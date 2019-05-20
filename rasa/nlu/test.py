import itertools
from collections import defaultdict, namedtuple

import json
import os
import logging
import numpy as np
import shutil
from typing import List, Optional, Text, Union, Dict
from tqdm import tqdm

from rasa.nlu import config, training_data, utils
from rasa.nlu.components import ComponentBuilder
from rasa.nlu.config import RasaNLUModelConfig
from rasa.nlu.extractors.crf_entity_extractor import CRFEntityExtractor
from rasa.nlu.model import Interpreter, Trainer, TrainingData

logger = logging.getLogger(__name__)

duckling_extractors = {"DucklingHTTPExtractor"}

known_duckling_dimensions = {
    "amount-of-money",
    "distance",
    "duration",
    "email",
    "number",
    "ordinal",
    "phone-number",
    "timezone",
    "temperature",
    "time",
    "url",
    "volume",
}

entity_processors = {"EntitySynonymMapper"}

CVEvaluationResult = namedtuple("Results", "train test")

IntentEvaluationResult = namedtuple(
    "IntentEvaluationResult", "target prediction message confidence"
)


def plot_confusion_matrix(
    cm, classes, normalize=False, title="Confusion matrix", cmap=None, zmin=1, out=None
) -> None:  # pragma: no cover
    """Print and plot the confusion matrix for the intent classification.
    Normalization can be applied by setting `normalize=True`."""
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    zmax = cm.max()
    plt.clf()
    if not cmap:
        cmap = plt.cm.Blues
    plt.imshow(
        cm,
        interpolation="nearest",
        cmap=cmap,
        aspect="auto",
        norm=LogNorm(vmin=zmin, vmax=zmax),
    )
    plt.title(title)
    plt.colorbar()
    tick_marks = np.arange(len(classes))
    plt.xticks(tick_marks, classes, rotation=90)
    plt.yticks(tick_marks, classes)

    if normalize:
        cm = cm.astype("float") / cm.sum(axis=1)[:, np.newaxis]
        logger.info("Normalized confusion matrix: \n{}".format(cm))
    else:
        logger.info("Confusion matrix, without normalization: \n{}".format(cm))

    thresh = cm.max() / 2.0
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        plt.text(
            j,
            i,
            cm[i, j],
            horizontalalignment="center",
            color="white" if cm[i, j] > thresh else "black",
        )

    plt.ylabel("True label")
    plt.xlabel("Predicted label")

    # save confusion matrix to file before showing it
    if out:
        fig = plt.gcf()
        fig.set_size_inches(20, 20)
        fig.savefig(out, bbox_inches="tight")


def plot_histogram(
    hist_data: List[List[float]], out: Optional[Text] = None
) -> None:  # pragma: no cover
    """Plot a histogram of the confidence distribution of the predictions in
    two columns.
    Wine-ish colour for the confidences of hits.
    Blue-ish colour for the confidences of misses.
    Saves the plot to a file."""
    import matplotlib.pyplot as plt

    colors = ["#009292", "#920000"]  #
    bins = [0.05 * i for i in range(1, 21)]

    plt.xlim([0, 1])
    plt.hist(hist_data, bins=bins, color=colors)
    plt.xticks(bins)
    plt.title("Intent Prediction Confidence Distribution")
    plt.xlabel("Confidence")
    plt.ylabel("Number of Samples")
    plt.legend(["hits", "misses"])

    if out:
        fig = plt.gcf()
        fig.set_size_inches(10, 10)
        fig.savefig(out, bbox_inches="tight")


def log_evaluation_table(
    report: Text, precision: float, f1: float, accuracy: float
) -> None:  # pragma: no cover
    """Log the sklearn evaluation metrics."""

    logger.info("F1-Score:  {}".format(f1))
    logger.info("Precision: {}".format(precision))
    logger.info("Accuracy:  {}".format(accuracy))
    logger.info("Classification report: \n{}".format(report))


def get_evaluation_metrics(targets, predictions, output_dict=False):
    """Compute the f1, precision, accuracy and summary report from sklearn."""
    from sklearn import metrics

    targets = clean_intent_labels(targets)
    predictions = clean_intent_labels(predictions)

    report = metrics.classification_report(
        targets, predictions, output_dict=output_dict
    )
    precision = metrics.precision_score(targets, predictions, average="weighted")
    f1 = metrics.f1_score(targets, predictions, average="weighted")
    accuracy = metrics.accuracy_score(targets, predictions)

    return report, precision, f1, accuracy


def remove_empty_intent_examples(intent_results):
    """Remove those examples without an intent."""

    filtered = []
    for r in intent_results:
        # substitute None values with empty string
        # to enable sklearn evaluation
        if r.prediction is None:
            r = r._replace(prediction="")

        if r.target != "" and r.target is not None:
            filtered.append(r)

    return filtered


def clean_intent_labels(labels):
    """Get rid of `None` intents. sklearn metrics do not support them."""
    return [l if l is not None else "" for l in labels]


def drop_intents_below_freq(td: TrainingData, cutoff: int = 5):
    """Remove intent groups with less than cutoff instances."""

    logger.debug("Raw data intent examples: {}".format(len(td.intent_examples)))
    keep_examples = [
        ex
        for ex in td.intent_examples
        if td.examples_per_intent[ex.get("intent")] >= cutoff
    ]

    return TrainingData(keep_examples, td.entity_synonyms, td.regex_features)


def save_json(data, filename):
    """Write out nlu classification to a file."""

    utils.write_to_file(filename, json.dumps(data, indent=4, ensure_ascii=False))


def collect_nlu_successes(intent_results, successes_filename):
    """Log messages which result in successful predictions
    and save them to file"""

    successes = [
        {
            "text": r.message,
            "intent": r.target,
            "intent_prediction": {"name": r.prediction, "confidence": r.confidence},
        }
        for r in intent_results
        if r.target == r.prediction
    ]

    if successes:
        save_json(successes, successes_filename)
        logger.info(
            "Model prediction successes saved to {}.".format(successes_filename)
        )
        logger.debug(
            "\n\nSuccessfully predicted the following intents: \n{}".format(successes)
        )
    else:
        logger.info("Your model made no successful predictions")


def collect_nlu_errors(intent_results, errors_filename):
    """Log messages which result in wrong predictions and save them to file"""

    errors = [
        {
            "text": r.message,
            "intent": r.target,
            "intent_prediction": {"name": r.prediction, "confidence": r.confidence},
        }
        for r in intent_results
        if r.target != r.prediction
    ]

    if errors:
        save_json(errors, errors_filename)
        logger.info("Model prediction errors saved to {}.".format(errors_filename))
        logger.debug(
            "\n\nThese intent examples could not be classified "
            "correctly: \n{}".format(errors)
        )
    else:
        logger.info("Your model made no errors")


def plot_intent_confidences(intent_results, intent_hist_filename):
    import matplotlib.pyplot as plt

    # create histogram of confidence distribution, save to file and display
    plt.gcf().clear()
    pos_hist = [r.confidence for r in intent_results if r.target == r.prediction]

    neg_hist = [r.confidence for r in intent_results if r.target != r.prediction]

    plot_histogram([pos_hist, neg_hist], intent_hist_filename)


def evaluate_intents(
    intent_results,
    report_folder,
    successes_filename,
    errors_filename,
    confmat_filename,
    intent_hist_filename,
):  # pragma: no cover
    """Creates a confusion matrix and summary statistics for intent predictions.
    Log samples which could not be classified correctly and save them to file.
    Creates a confidence histogram which is saved to file.
    Wrong and correct prediction confidences will be
    plotted in separate bars of the same histogram plot.
    Only considers those examples with a set intent.
    Others are filtered out. Returns a dictionary of containing the
    evaluation result."""

    # remove empty intent targets
    num_examples = len(intent_results)
    intent_results = remove_empty_intent_examples(intent_results)

    logger.info(
        "Intent Evaluation: Only considering those "
        "{} examples that have a defined intent out "
        "of {} examples".format(len(intent_results), num_examples)
    )

    targets, predictions = _targets_predictions_from(intent_results)

    if report_folder:
        report, precision, f1, accuracy = get_evaluation_metrics(
            targets, predictions, output_dict=True
        )

        report_filename = os.path.join(report_folder, "intent_report.json")

        save_json(report, report_filename)
        logger.info("Classification report saved to {}.".format(report_filename))

    else:
        report, precision, f1, accuracy = get_evaluation_metrics(targets, predictions)
        log_evaluation_table(report, precision, f1, accuracy)

    if successes_filename:
        # save classified samples to file for debugging
        collect_nlu_successes(intent_results, successes_filename)

    if errors_filename:
        # log and save misclassified samples to file for debugging
        collect_nlu_errors(intent_results, errors_filename)

    if confmat_filename:
        from sklearn.metrics import confusion_matrix
        from sklearn.utils.multiclass import unique_labels
        import matplotlib.pyplot as plt

        cnf_matrix = confusion_matrix(targets, predictions)
        labels = unique_labels(targets, predictions)
        plot_confusion_matrix(
            cnf_matrix,
            classes=labels,
            title="Intent Confusion matrix",
            out=confmat_filename,
        )
        plt.show()

        plot_intent_confidences(intent_results, intent_hist_filename)

        plt.show()

    predictions = [
        {
            "text": res.message,
            "intent": res.target,
            "predicted": res.prediction,
            "confidence": res.confidence,
        }
        for res in intent_results
    ]

    return {
        "predictions": predictions,
        "report": report,
        "precision": precision,
        "f1_score": f1,
        "accuracy": accuracy,
    }


def merge_labels(aligned_predictions, extractor=None):
    """Concatenates all labels of the aligned predictions.
    Takes the aligned prediction labels which are grouped for each message
    and concatenates them."""

    if extractor:
        label_lists = [ap["extractor_labels"][extractor] for ap in aligned_predictions]
    else:
        label_lists = [ap["target_labels"] for ap in aligned_predictions]

    flattened = list(itertools.chain(*label_lists))
    return np.array(flattened)


def substitute_labels(labels, old, new):
    """Replaces label names in a list of labels."""
    return [new if label == old else label for label in labels]


def evaluate_entities(
    targets, predictions, tokens, extractors, report_folder
):  # pragma: no cover
    """Creates summary statistics for each entity extractor.
    Logs precision, recall, and F1 per entity type for each extractor."""

    aligned_predictions = align_all_entity_predictions(
        targets, predictions, tokens, extractors
    )
    merged_targets = merge_labels(aligned_predictions)
    merged_targets = substitute_labels(merged_targets, "O", "no_entity")

    result = {}

    for extractor in extractors:
        merged_predictions = merge_labels(aligned_predictions, extractor)
        merged_predictions = substitute_labels(merged_predictions, "O", "no_entity")
        logger.info("Evaluation for entity extractor: {} ".format(extractor))
        if report_folder:
            report, precision, f1, accuracy = get_evaluation_metrics(
                merged_targets, merged_predictions, output_dict=True
            )

            report_filename = extractor + "_report.json"
            extractor_report = os.path.join(report_folder, report_filename)

            save_json(report, extractor_report)
            logger.info(
                "Classification report for '{}' saved to '{}'."
                "".format(extractor, extractor_report)
            )

        else:
            report, precision, f1, accuracy = get_evaluation_metrics(
                merged_targets, merged_predictions
            )
            log_evaluation_table(report, precision, f1, accuracy)

        result[extractor] = {
            "report": report,
            "precision": precision,
            "f1_score": f1,
            "accuracy": accuracy,
        }

    return result


def is_token_within_entity(token, entity):
    """Checks if a token is within the boundaries of an entity."""
    return determine_intersection(token, entity) == len(token.text)


def does_token_cross_borders(token, entity):
    """Checks if a token crosses the boundaries of an entity."""

    num_intersect = determine_intersection(token, entity)
    return 0 < num_intersect < len(token.text)


def determine_intersection(token, entity):
    """Calculates how many characters a given token and entity share."""

    pos_token = set(range(token.offset, token.end))
    pos_entity = set(range(entity["start"], entity["end"]))
    return len(pos_token.intersection(pos_entity))


def do_entities_overlap(entities):
    """Checks if entities overlap.
    I.e. cross each others start and end boundaries.
    :param entities: list of entities
    :return: boolean
    """

    sorted_entities = sorted(entities, key=lambda e: e["start"])
    for i in range(len(sorted_entities) - 1):
        curr_ent = sorted_entities[i]
        next_ent = sorted_entities[i + 1]
        if (
            next_ent["start"] < curr_ent["end"]
            and next_ent["entity"] != curr_ent["entity"]
        ):
            logger.warn("Overlapping entity {} with {}".format(curr_ent, next_ent))
            return True

    return False


def find_intersecting_entites(token, entities):
    """Finds the entities that intersect with a token.
    :param token: a single token
    :param entities: entities found by a single extractor
    :return: list of entities
    """

    candidates = []
    for e in entities:
        if is_token_within_entity(token, e):
            candidates.append(e)
        elif does_token_cross_borders(token, e):
            candidates.append(e)
            logger.debug(
                "Token boundary error for token {}({}, {}) "
                "and entity {}"
                "".format(token.text, token.offset, token.end, e)
            )
    return candidates


def pick_best_entity_fit(token, candidates):
    """Determines the token label given intersecting entities.
    :param token: a single token
    :param candidates: entities found by a single extractor
    :return: entity type
    """

    if len(candidates) == 0:
        return "O"
    elif len(candidates) == 1:
        return candidates[0]["entity"]
    else:
        best_fit = np.argmax([determine_intersection(token, c) for c in candidates])
        return candidates[best_fit]["entity"]


def determine_token_labels(token, entities, extractors):
    """Determines the token label given entities that do not overlap.
    Args:
        token: a single token
        entities: entities found by a single extractor
        extractors: list of extractors
    Returns:
        entity type
    """

    if len(entities) == 0:
        return "O"
    if not do_extractors_support_overlap(extractors) and do_entities_overlap(entities):
        raise ValueError("The possible entities should not overlap")

    candidates = find_intersecting_entites(token, entities)
    return pick_best_entity_fit(token, candidates)


def do_extractors_support_overlap(extractors):
    """Checks if extractors support overlapping entities
    """
    if extractors is None:
        return False
    return CRFEntityExtractor.name not in extractors


def align_entity_predictions(targets, predictions, tokens, extractors):
    """Aligns entity predictions to the message tokens.
    Determines for every token the true label based on the
    prediction targets and the label assigned by each
    single extractor.
    :param targets: list of target entities
    :param predictions: list of predicted entities
    :param tokens: original message tokens
    :param extractors: the entity extractors that should be considered
    :return: dictionary containing the true token labels and token labels
             from the extractors
    """

    true_token_labels = []
    entities_by_extractors = {extractor: [] for extractor in extractors}
    for p in predictions:
        entities_by_extractors[p["extractor"]].append(p)
    extractor_labels = {extractor: [] for extractor in extractors}
    for t in tokens:
        true_token_labels.append(determine_token_labels(t, targets, None))
        for extractor, entities in entities_by_extractors.items():
            extracted = determine_token_labels(t, entities, extractor)
            extractor_labels[extractor].append(extracted)

    return {
        "target_labels": true_token_labels,
        "extractor_labels": dict(extractor_labels),
    }


def align_all_entity_predictions(targets, predictions, tokens, extractors):
    """ Aligns entity predictions to the message tokens for the whole dataset
        using align_entity_predictions
    :param targets: list of lists of target entities
    :param predictions: list of lists of predicted entities
    :param tokens: list of original message tokens
    :param extractors: the entity extractors that should be considered
    :return: list of dictionaries containing the true token labels and token
             labels from the extractors
    """

    aligned_predictions = []
    for ts, ps, tks in zip(targets, predictions, tokens):
        aligned_predictions.append(align_entity_predictions(ts, ps, tks, extractors))

    return aligned_predictions


def get_intent_targets(test_data):  # pragma: no cover
    """Extracts intent targets from the test data."""
    return [e.get("intent", "") for e in test_data.training_examples]


def get_entity_targets(test_data):
    """Extracts entity targets from the test data."""
    return [e.get("entities", []) for e in test_data.training_examples]


def extract_intent(result):  # pragma: no cover
    """Extracts the intent from a parsing result."""
    return result.get("intent", {}).get("name")


def extract_entities(result):  # pragma: no cover
    """Extracts entities from a parsing result."""
    return result.get("entities", [])


def extract_message(result):  # pragma: no cover
    """Extracts the original message from a parsing result."""
    return result.get("text", {})


def extract_confidence(result):  # pragma: no cover
    """Extracts the confidence from a parsing result."""
    return result.get("intent", {}).get("confidence")


def get_predictions(interpreter, test_data, intent_targets):  # pragma: no cover
    """Run the model for the test set and extracts intents and entities.

    Return intent and entity predictions, the original messages and the
    confidences of the predictions."""

    logger.info("Running model for predictions:")

    intent_results, entity_predictions, tokens = [], [], []

    # cycle makes sure we use all training examples if there are
    # no intent targets
    samples_with_targets = zip(
        test_data.training_examples, itertools.cycle(intent_targets)
    )
    same_intents = {"I_dont_know_about_where_you_live_but_in_my_world_its_always_sunny": 0,
                    "Its_sunny_where_I_live": 0,
                    "The_flowers_are_starting_to_blossom_so_it_must_be_spring": 0,
                    "Spring_is_here": 0,
                    "The_engineers_at_Rasa": 1,
                    "One_of_the_smart_engineers_at_Rasa": 1,
                    "Im_great_Thanks_for_asking": 2,
                    "Im_good_thanks": 2,
                    "A_little_bit_cold_otherwise_fine": 2,
                    "Im_Sara_the_Rasa_bot_At_the_same_time_Im_also_the_Rasa_mascot": 3,
                    "Im_both_the_Rasa_bot_and_the_Rasa_mascot_My_name_is_Sara": 3,
                    "Yes_Im_a_bot": 4,
                    "Yep_you_guessed_it_Im_a_bot": 4,
                    "I_am_indeed_a_bot": 4,
                    "42": 5,
                    "Old_enough_to_be_a_bot": 5,
                    "Age_is_just_an_issue_of_mind_over_matter_If_you_dont_mind_it_doesnt_matter": 5,
                    "Never_ask_a_chatbot_her_age": 5,
                    "My_first_git_commit_was_many_moons_ago": 5,
                    "Why_do_you_ask_Are_my_wrinkles_showing": 5,
                    "Ive_hit_the_age_where_I_actively_try_to_forget_how_old_I_am": 5,
                    "Why_are_eggs_not_very_much_into_jokes_Because_they_could_crack_up": 6,
                    "Do_you_know_a_trees_favorite_drink_Root_beer": 6,
                    "Why_do_the_French_like_to_eat_snails_so_much_They_cant_stand_fast_food": 6,
                    "Why_did_the_robot_get_angry_Because_someone_kept_pushing_its_buttons": 6,
                    "What_do_you_call_a_pirate_droid_Arrrr2D2": 6,
                    "Why_did_the_robot_cross_the_road_Because_he_was_programmed_to": 6,
                    "Its_party_time": 7,
                    "Time_is_a_human_construct_youll_have_to_tell_me": 7,
                    "Its_five_oclock_somewhere": 7,
                    "In_an_ever_expanding_universe_the_real_question_is_what_time_isnt_it": 7,
                    "Thats_hard_to_say_its_different_all_over_the_world": 7,
                    "I_can_spell_baguette_in_French_but_unfortunately_English_is_the_only_language_I_can_answer_you_in": 8,
                    "I_am_in_the_process_of_learning_but_at_the_moment_I_can_only_speak_English": 8,
                    "Binary_code_and_the_language_of_love_And_English": 8,
                    "I_was_written_in_python_but_for_your_convenience_Ill_translate_to_English": 8,
                    "Thats_not_very_nice": 9,
                    "That_wasnt_very_nice_Perhaps_try_an_anger_management_class": 9,
                    "Ill_pretend_I_didnt_process_that_mean_comment": 9,
                    "Im_sorry_I_cant_recommend_you_a_restaurant_as_I_usually_cook_at_home": 10,
                    "Im_sorry_Im_not_getting_taste_buds_for_another_few_updates": 10,
                    "Id_need_some_more_data_If_you_lick_the_monitor_perhaps_I_can_evaluate_your_taste_buds": 10,
                    "I_hope_you_are_being_yourself": 11,
                    "Who_do_you_think_you_are": 11,
                    "Unfortunately_I_havent_been_programmed_with_the_amount_of_necessary_philosophy_knowledge_to_answer_that": 11,
                    "It_most_probably_is_the_one_that_your_parents_have_chosen_for_you": 12,
                    "Youre_the_second_person_now_to_ask_me_that_Rihanna_was_the_first": 12,
                    "Id_tell_you_but_theres_restricted_access_to_that_chunk_of_memory": 12,
                    "Believe_it_or_not_I_actually_am_not_spying_on_your_personal_information": 12,
                    "Thank_you_It_is_a_pleasure_to_meet_you_as_well": 13,
                    "It_is_nice_to_meet_you_too": 13,
                    "Pleased_to_meet_you_too": 13,
                    "Likewise": 13,
                    "Its_always_a_pleasure_to_meet_new_people": 13,
                    "Nice_to_meet_you_too_Happy_to_be_of_help": 13,
                    "I_was_born_in_Berlin_but_I_consider_myself_a_citizen_of_the_world": 14,
                    "I_was_born_in_the_coolest_city_on_Earth_in_Berlin": 14,
                    "My_developers_come_from_all_over_the_world": 14,
                    "I_was_taught_not_to_give_out_my_address_on_the_internet": 14,
                    "My_address_starts_with_githubcom": 14,
                    "Well_when_two_chatbots_love_each_other_very_much": 15,
                    "They_always_ask_how_I_was_built_but_never_how_I_am": 15,
                    "I_was_made_by_software_engineers_but_hard_work_is_what_built_me": 15,
                    "Im_building_myself_every_day_Ive_been_working_out_did_you_notice": 15,
                    }
    # new_targets = []
    # for t, p in zip(targets, predictions):
    #     if p in same_intents.keys() and t in same_intents.keys() and same_intents[t] == same_intents[p]:
    #         new_targets.append(p)
    #     else:
    #         # if t != p and p in same_intents.keys() and t in same_intents.keys():
    #         #     print(t)
    #         #     print(p)
    #         #     exit()
    #         new_targets.append(t)
    # targets = new_targets
    # print(targets[0])
    # print(predictions[0])
    # exit()

    for e, target in tqdm(samples_with_targets, total=len(test_data.training_examples)):
        res = interpreter.parse(e.text, only_output_properties=False)

        if is_intent_classifier_present(interpreter):
            p = extract_intent(res)
            t = target
            if p in same_intents.keys() and t in same_intents.keys() and same_intents[t] == same_intents[p]:
                target = p

            intent_results.append(
                IntentEvaluationResult(
                    target,
                    extract_intent(res),
                    extract_message(res),
                    extract_confidence(res),
                )
            )

        entity_predictions.append(extract_entities(res))
        try:
            tokens.append(res["tokens"])
        except KeyError:
            logger.debug(
                "No tokens present, which is fine if you don't "
                "have a tokenizer in your pipeline"
            )

    return intent_results, entity_predictions, tokens


def get_entity_extractors(interpreter):
    """Finds the names of entity extractors used by the interpreter.
    Processors are removed since they do not
    detect the boundaries themselves."""

    extractors = set([c.name for c in interpreter.pipeline if "entities" in c.provides])
    return extractors - entity_processors


def is_intent_classifier_present(interpreter):
    """Checks whether intent classifier is present"""

    intent_classifier = [c.name for c in interpreter.pipeline if "intent" in c.provides]
    return intent_classifier != []


def combine_extractor_and_dimension_name(extractor, dim):
    """Joins the duckling extractor name with a dimension's name."""
    return "{} ({})".format(extractor, dim)


def get_duckling_dimensions(interpreter, duckling_extractor_name):
    """Gets the activated dimensions of a duckling extractor.
    If there are no activated dimensions, it uses all known
    dimensions as a fallback."""

    component = find_component(interpreter, duckling_extractor_name)
    if component.component_config["dimensions"]:
        return component.component_config["dimensions"]
    else:
        return known_duckling_dimensions


def find_component(interpreter, component_name):
    """Finds a component in a pipeline."""

    for c in interpreter.pipeline:
        if c.name == component_name:
            return c
    return None


def remove_duckling_extractors(extractors):
    """Removes duckling exctractors"""
    used_duckling_extractors = duckling_extractors.intersection(extractors)
    for duckling_extractor in used_duckling_extractors:
        logger.info("Skipping evaluation of {}".format(duckling_extractor))
        extractors.remove(duckling_extractor)

    return extractors


def remove_duckling_entities(entity_predictions):
    """Removes duckling entity predictions"""

    patched_entity_predictions = []
    for entities in entity_predictions:
        patched_entities = []
        for e in entities:
            if e["extractor"] not in duckling_extractors:
                patched_entities.append(e)
        patched_entity_predictions.append(patched_entities)

    return patched_entity_predictions


def run_evaluation(
    data_path: Text,
    model_path: Text,
    report: Optional[Text] = None,
    successes: Optional[Text] = None,
    errors: Optional[Text] = "errors.json",
    confmat: Optional[Text] = None,
    histogram: Optional[Text] = None,
    component_builder: Optional[ComponentBuilder] = None,
) -> Dict:  # pragma: no cover
    """
    Evaluate intent classification and entity extraction.

    :param data_path: path to the test data
    :param model_path: path to the model
    :param report: path to folder where reports are stored
    :param successes: path to file that will contain success cases
    :param errors: path to file that will contain error cases
    :param confmat: path to file that will show the confusion matrix
    :param histogram: path fo file that will show a histogram
    :param component_builder: component builder

    :return: dictionary containing evaluation results
    """

    # get the metadata config from the package data
    interpreter = Interpreter.load(model_path, component_builder)
    test_data = training_data.load_data(data_path, interpreter.model_metadata.language)

    extractors = get_entity_extractors(interpreter)

    if is_intent_classifier_present(interpreter):
        intent_targets = get_intent_targets(test_data)
    else:
        intent_targets = [None] * len(test_data.training_examples)

    intent_results, entity_predictions, tokens = get_predictions(
        interpreter, test_data, intent_targets
    )

    if duckling_extractors.intersection(extractors):
        entity_predictions = remove_duckling_entities(entity_predictions)
        extractors = remove_duckling_extractors(extractors)

    result = {"intent_evaluation": None, "entity_evaluation": None}

    if report:
        utils.create_dir(report)

    if is_intent_classifier_present(interpreter):

        logger.info("Intent evaluation results:")
        result["intent_evaluation"] = evaluate_intents(
            intent_results, report, successes, errors, confmat, histogram
        )

    if extractors:
        entity_targets = get_entity_targets(test_data)

        logger.info("Entity evaluation results:")
        result["entity_evaluation"] = evaluate_entities(
            entity_targets, entity_predictions, tokens, extractors, report
        )

    return result


def generate_folds(n, td):
    """Generates n cross validation folds for training data td."""

    from sklearn.model_selection import StratifiedKFold

    skf = StratifiedKFold(n_splits=n, shuffle=True)
    x = td.intent_examples
    y = [example.get("intent") for example in x]
    for i_fold, (train_index, test_index) in enumerate(skf.split(x, y)):
        logger.debug("Fold: {}".format(i_fold))
        train = [x[i] for i in train_index]
        test = [x[i] for i in test_index]
        yield (
            TrainingData(
                training_examples=train,
                entity_synonyms=td.entity_synonyms,
                regex_features=td.regex_features,
            ),
            TrainingData(
                training_examples=test,
                entity_synonyms=td.entity_synonyms,
                regex_features=td.regex_features,
            ),
        )


def combine_result(intent_results, entity_results, interpreter, data):
    """Combines intent and entity result for crossvalidation folds"""

    intent_current_result, entity_current_result = compute_metrics(interpreter, data)

    intent_results = {
        k: v + intent_results[k] for k, v in intent_current_result.items()
    }

    for k, v in entity_current_result.items():
        entity_results[k] = {
            key: val + entity_results[k][key] for key, val in v.items()
        }

    return intent_results, entity_results


def cross_validate(
    data: TrainingData, n_folds: int, nlu_config: Union[RasaNLUModelConfig, Text]
) -> CVEvaluationResult:
    """Stratified cross validation on data.

    Args:
        data: Training Data
        n_folds: integer, number of cv folds
        nlu_config: nlu config file

    Returns:
        dictionary with key, list structure, where each entry in list
              corresponds to the relevant result for one fold
    """
    from collections import defaultdict
    import tempfile

    if isinstance(nlu_config, str):
        nlu_config = config.load(nlu_config)

    trainer = Trainer(nlu_config)
    intent_train_results = defaultdict(list)
    intent_test_results = defaultdict(list)
    entity_train_results = defaultdict(lambda: defaultdict(list))
    entity_test_results = defaultdict(lambda: defaultdict(list))
    tmp_dir = tempfile.mkdtemp()

    for train, test in generate_folds(n_folds, data):
        interpreter = trainer.train(train)

        # calculate train accuracy
        intent_train_results, entity_train_results = combine_result(
            intent_train_results, entity_train_results, interpreter, train
        )
        # calculate test accuracy
        intent_test_results, entity_test_results = combine_result(
            intent_test_results, entity_test_results, interpreter, test
        )

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return (
        CVEvaluationResult(dict(intent_train_results), dict(intent_test_results)),
        CVEvaluationResult(dict(entity_train_results), dict(entity_test_results)),
    )


def _targets_predictions_from(intent_results):
    return zip(*[(r.target, r.prediction) for r in intent_results])


def compute_metrics(interpreter, corpus):
    """Computes metrics for intent classification and entity extraction."""

    intent_targets = get_intent_targets(corpus)
    intent_results, entity_predictions, tokens = get_predictions(
        interpreter, corpus, intent_targets
    )
    intent_results = remove_empty_intent_examples(intent_results)

    intent_metrics = _compute_intent_metrics(intent_results, interpreter, corpus)
    entity_metrics = _compute_entity_metrics(
        entity_predictions, tokens, interpreter, corpus
    )

    return intent_metrics, entity_metrics


def _compute_intent_metrics(intent_results, interpreter, corpus):
    """Computes intent evaluation metrics for a given corpus and
    returns the results
    """
    # compute fold metrics
    targets, predictions = _targets_predictions_from(intent_results)
    _, precision, f1, accuracy = get_evaluation_metrics(targets, predictions)

    return {"Accuracy": [accuracy], "F1-score": [f1], "Precision": [precision]}


def _compute_entity_metrics(entity_predictions, tokens, interpreter, corpus):
    """Computes entity evaluation metrics for a given corpus and
    returns the results
    """
    entity_results = defaultdict(lambda: defaultdict(list))
    extractors = get_entity_extractors(interpreter)

    if duckling_extractors.intersection(extractors):
        entity_predictions = remove_duckling_entities(entity_predictions)
        extractors = remove_duckling_extractors(extractors)

    if not extractors:
        return entity_results

    entity_targets = get_entity_targets(corpus)

    aligned_predictions = align_all_entity_predictions(
        entity_targets, entity_predictions, tokens, extractors
    )

    merged_targets = merge_labels(aligned_predictions)
    merged_targets = substitute_labels(merged_targets, "O", "no_entity")

    for extractor in extractors:
        merged_predictions = merge_labels(aligned_predictions, extractor)
        merged_predictions = substitute_labels(merged_predictions, "O", "no_entity")
        _, precision, f1, accuracy = get_evaluation_metrics(
            merged_targets, merged_predictions
        )
        entity_results[extractor]["Accuracy"].append(accuracy)
        entity_results[extractor]["F1-score"].append(f1)
        entity_results[extractor]["Precision"].append(precision)

    return entity_results


def return_results(results, dataset_name):
    """Returns results of crossvalidation
    :param results: dictionary of results returned from cv
    :param dataset_name: string of which dataset the results are from, e.g.
                    test/train
    """

    for k, v in results.items():
        logger.info(
            "{} {}: {:.3f} ({:.3f})".format(dataset_name, k, np.mean(v), np.std(v))
        )


def return_entity_results(results, dataset_name):
    """Returns entity results of crossvalidation
    :param results: dictionary of dictionaries of results returned from cv
    :param dataset_name: string of which dataset the results are from, e.g.
                    test/train
    """
    for extractor, result in results.items():
        logger.info("Entity extractor: {}".format(extractor))
        return_results(result, dataset_name)


if __name__ == "__main__":
    raise RuntimeError(
        "Calling `rasa.nlu.test` directly is no longer supported. Please use "
        "`rasa test` to test a combined Core and NLU model or `rasa test nlu` "
        "to test an NLU model."
    )
